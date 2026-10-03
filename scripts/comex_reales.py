#!/usr/bin/env python3
"""
Job desatendido: lee facturas de despacho/forwarder desde Odoo (SOLO LECTURA)
y escribe comex_odoo_real.json en SharePoint, al lado de comex_data.json,
para que la herramienta COMEX lo lea como overlay.

FASE 1 (gastosReal) -- implementada.
FASE 2 (nacReal) -- implementada. Los codigos de cuenta del handoff
(118001, 114101, 114103, 114307, 114601, 1142xx, 211101, 118005, 118006)
SI existen, planos, en el plan de cuentas real de la compania 6 de PROD
(la busqueda jerarquica anterior -- "1.1.4.02.010" -- consultaba la
compania de test, no la 6). Verificado en vivo contra el DI real de
LUMI_302 (account.move id 44648, company 6, x_lumi_tc_historico=1512):
los 8 valores (noRecup, iva, pIva, pGan, impInt, iibb, VA, desembolso)
coinciden con los esperados del handoff. Si un embarque no tiene DI
posteado, o no hay TC resoluble, o el control de gate (noRecup+credito
vs desembolso) no cuadra dentro de 1 USD, nacReal queda en None -- nunca
se estima.

EL TC SALE DE x_lumi_cohorte, NO DE account.move.x_lumi_tc_historico (fix
03/10/2026, diagnosticado en vivo sobre LUMI_304): el circuito V4
(Studio) introdujo un modelo `x_lumi_cohorte` que linkea cohorte -> DI
(x_di_move_id) -> TC aduanero (x_tc_aduanero), y para cohortes recientes
(LUMI_297, LUMI_304) ya NO deja el x_lumi_tc_historico cargado en el
propio move -- queda en 0 -- aunque el TC real SI esta en
x_lumi_cohorte.x_tc_aduanero. resolver_di_cohorte() busca primero ahi
(por x_codigo) y solo cae al metodo viejo (ref/name del DI +
x_lumi_tc_historico) si la cohorte no tiene x_lumi_cohorte (anteriores
al circuito V4). OJO -- el gate NO protege contra un TC mal cargado en
x_lumi_cohorte: compara saldos ARS que por partida doble ya cuadran
entre si ANTES de dividir por el TC, asi que cualquier TC (bien o mal
cargado, mientras no sea 0/vacio) pasa el gate igual -- solo corre el
resultado en USD a una escala distinta. El gate detecta un mapeo de
cuentas roto, no un TC erroneo.

Escribe comex_odoo_real.json (reescribe el archivo completo en cada corrida).

SYNC TABLERO ODOO (despues de lo anterior, ver scripts/comex_odoo_sync.py): upsert de los embarques
LUMI_ al modelo x.comex.embarque del modulo comex_dashboard (es el UNICO modelo de Odoo donde escribe)
y writeback a comex_data.json de los 3 campos de mano (notas, despa, opDesp) que se editaron en Odoo,
con verificacion por diff. Si el modulo no esta instalado en Odoo, el paso se omite sin error.
COMEX_SYNC_DRY_RUN=1 muestra lo que haria sin escribir ni en Odoo ni en SharePoint.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import comex_odoo_sync

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
TRACKER_FILE = "comex_data.json"
TRACKER_BACKUP_FILE = "comex_data.pre_odoo_sync.json"
SYNC_DRY_RUN = os.environ.get("COMEX_SYNC_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

MUNDO_COMEX_VAT = "30717845419"
# ilike 'trice%' (no '%trice%'): una busqueda por substring sin ancla de
# inicio matchea falsos positivos reales, ej. un partner persona humana
# "Martin GabrielPetricevich" (contiene "tricev" como substring).
TRICE_NAME_PATTERN = "trice%"

COHORTE_RE = re.compile(r"(LUMI|LUPE)[_ ]?0?(\d{2,3})", re.IGNORECASE)

# FASE 2 -- codigos de cuenta reales de la compania 6 (verificados contra
# el DI de LUMI_302, account.move id 44648).
NAC_COMPANY_ID = 6
NAC_CUENTA_NORECUP = "118001"  # Cuenta Puente Mercaderias
NAC_CUENTA_IVA = "114101"  # IVA Credito Fiscal
NAC_CUENTA_PIVA = "114103"  # Percepcion de IVA Sufrida
NAC_CUENTA_PGAN = "114307"  # Percepcion de Ganancias Sufrida
NAC_CUENTA_IMPINT = "114601"  # Impuestos Internos - Pago a Cuenta
NAC_CUENTA_IIBB_PREFIX = "1142"  # 114203-114226, percepciones IIBB por jurisdiccion
NAC_CUENTAS_BASEIVA = ("118005", "118006")  # Base Imponible IVA 10,5%/21% Importacion
NAC_CUENTA_DESEMBOLSO = "211101"  # Proveedores (liability_payable, credito-normal)
NAC_SIM_USD = 10.0
NAC_GATE_TOLERANCIA_USD = 1.0


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


def resolver_di_cohorte(uid, cohorte):
    """Busca el DI (despacho de importacion) de una cohorte LUMI_ en la
    compania 6 real de PROD. Devuelve (move_id, name, fecha, tc), o
    (None, None, None, None) si no hay TC resoluble -- en ese caso nacReal
    queda pendiente, nunca se estima.

    Primero por x_lumi_cohorte (x_codigo = cohorte): el modelo del circuito
    V4 (Studio) que linkea cohorte -> DI (x_di_move_id) -> TC aduanero
    (x_tc_aduanero). Es MAS CONFIABLE que el metodo viejo: verificado en
    vivo que para cohortes recientes (LUMI_297, LUMI_304) el `ref` del DI
    puede venir vacio o su `x_lumi_tc_historico` en 0 aunque el TC real SI
    este cargado en x_lumi_cohorte.x_tc_aduanero (LUMI_304: DI 48936,
    x_lumi_tc_historico=0, x_tc_aduanero=1524.5 -- con ese TC el gate de
    build_nac_real cuadra al centavo). Si la cohorte existe en
    x_lumi_cohorte pero sin TC aduanero cargado, se la deja pendiente ahi
    mismo (no cae al metodo viejo: si existiera un x_lumi_tc_historico
    viejo en el DI no hay que usarlo, ya quedo superado por este modelo).

    Si la cohorte NO tiene x_lumi_cohorte (cohortes de antes del circuito
    V4, ej. LUMI_302 si se borrara ese registro), cae al metodo viejo:
    ref/name del account.move + su x_lumi_tc_historico.
    """
    cohortes = odoo_execute_kw(
        uid,
        "x_lumi_cohorte",
        "search_read",
        [[["x_codigo", "=", cohorte], ["x_company_id", "=", NAC_COMPANY_ID]]],
        {"fields": ["x_di_move_id", "x_tc_aduanero", "x_fecha_oficializacion", "x_despacho"], "limit": 1},
    )
    if cohortes:
        c = cohortes[0]
        tc = c.get("x_tc_aduanero")
        move = c.get("x_di_move_id")
        if move and tc and c.get("x_despacho"):
            return move[0], "DI " + c["x_despacho"], c.get("x_fecha_oficializacion"), float(tc)
        return None, None, None, None

    dis = odoo_execute_kw(
        uid,
        "account.move",
        "search_read",
        [[["ref", "ilike", cohorte], ["name", "ilike", "DI "]]],
        {
            "fields": ["name", "date", "x_lumi_tc_historico"],
            "context": {"allowed_company_ids": [NAC_COMPANY_ID]},
            "limit": 5,
        },
    )
    for di in dis:
        tc = di.get("x_lumi_tc_historico")
        if tc:
            return di["id"], di["name"], di.get("date"), float(tc)
    return None, None, None, None


def build_nac_real(uid, cohorte):
    """FASE 2: recalcula nacReal a partir del DI real de la cohorte en la
    compania 6. Devuelve None (deja pendiente, no estima) si no hay DI
    con TC, o si el control de gate no cuadra dentro de NAC_GATE_TOLERANCIA_USD."""
    move_id, name, fecha, tc = resolver_di_cohorte(uid, cohorte)
    if not move_id or not tc:
        return None

    lines = odoo_execute_kw(
        uid,
        "account.move.line",
        "search_read",
        [[["move_id", "=", move_id]]],
        {
            "fields": ["account_id", "debit", "credit"],
            "context": {"allowed_company_ids": [NAC_COMPANY_ID]},
            "limit": 200,
        },
    )
    acc_ids = sorted({l["account_id"][0] for l in lines if l.get("account_id")})
    if not acc_ids:
        return None
    accs = odoo_execute_kw(
        uid,
        "account.account",
        "read",
        [acc_ids, ["code"]],
        {"context": {"allowed_company_ids": [NAC_COMPANY_ID]}},
    )
    code_de_id = {a["id"]: a["code"] for a in accs}

    neto = {}
    for l in lines:
        if not l.get("account_id"):
            continue
        code = code_de_id.get(l["account_id"][0])
        if not code:
            continue
        neto[code] = neto.get(code, 0.0) + (l["debit"] - l["credit"])

    no_recup_ars = neto.get(NAC_CUENTA_NORECUP, 0.0)
    iva_ars = neto.get(NAC_CUENTA_IVA, 0.0)
    p_iva_ars = neto.get(NAC_CUENTA_PIVA, 0.0)
    p_gan_ars = neto.get(NAC_CUENTA_PGAN, 0.0)
    imp_int_ars = neto.get(NAC_CUENTA_IMPINT, 0.0)
    iibb_ars = sum(v for c, v in neto.items() if c.startswith(NAC_CUENTA_IIBB_PREFIX))
    base_iva_ars = sum(neto.get(c, 0.0) for c in NAC_CUENTAS_BASEIVA)
    # 211101 es a pagar (liability_payable, credito-normal): el saldo que
    # importa para el desembolso es credito-debito, signo invertido
    # respecto del resto de las cuentas de esta formula (todas
    # asset_current, debito-normal).
    saldo_proveedores_ars = -neto.get(NAC_CUENTA_DESEMBOLSO, 0.0)

    sim_ars = NAC_SIM_USD * tc
    va_ars = base_iva_ars - (no_recup_ars - sim_ars)
    desembolso_ars = saldo_proveedores_ars - base_iva_ars

    no_recup = no_recup_ars / tc
    iva = iva_ars / tc
    p_iva = p_iva_ars / tc
    p_gan = p_gan_ars / tc
    imp_int = imp_int_ars / tc
    iibb = iibb_ars / tc
    va = va_ars / tc
    desembolso = desembolso_ars / tc
    credito = iva + p_iva + p_gan + imp_int + iibb

    gate_diff = abs(no_recup + credito - desembolso)
    if gate_diff > NAC_GATE_TOLERANCIA_USD:
        print(
            "WARN: %s nacReal no cuadra (noRecup+credito vs desembolso difieren USD %.2f), se omite"
            % (cohorte, gate_diff),
            file=sys.stderr,
        )
        return None

    despacho = name[len("DI "):].strip() if name.startswith("DI ") else name

    return {
        "fecha": fecha,
        "di": despacho,
        "tc": tc,
        "VA": round(va),
        "sim": int(NAC_SIM_USD),
        "noRecup": round(no_recup),
        "iva": round(iva),
        "pIva": round(p_iva),
        "pGan": round(p_gan),
        "iibb": round(iibb),
        "impInt": round(imp_int),
        "credito": round(credito),
        "desembolso": round(desembolso),
        "src": "Odoo DI move %d · company %d · TC DI" % (move_id, NAC_COMPANY_ID),
    }


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
                "nacReal": build_nac_real(uid, cohorte),
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


def graph_site_id(token):
    site = graph_request(token, "/sites/%s:%s" % (SP_HOST, SP_SITE))
    site_id = site.get("id")
    if not site_id:
        raise RuntimeError("Graph: no encuentro el sitio de SharePoint %s" % SP_SITE)
    return site_id


def graph_put_file(token, site_id, filename, body_bytes, if_match=None):
    """PUT del archivo completo. Con if_match, un 412 (el archivo cambio) lanza ConflictoTracker."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if if_match:
        headers["If-Match"] = if_match
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/sites/%s/drive/root:/%s:/content" % (site_id, filename),
        data=body_bytes,
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status not in (200, 201):
                raise RuntimeError("Graph: PUT de %s devolvio status %s" % (filename, r.status))
    except urllib.error.HTTPError as e:
        if e.code == 412:
            raise comex_odoo_sync.ConflictoTracker() from e
        raise


def graph_get_json_con_etag(token, site_id, filename):
    """(contenido parseado, eTag). El eTag se toma de los metadatos ANTES de bajar el contenido: si el
    archivo cambia entre medio, el PUT con If-Match falla con 412 en vez de pisar la version nueva."""
    meta = graph_request(token, "/sites/%s/drive/root:/%s" % (site_id, filename))
    etag = meta.get("eTag")
    url = meta.get("@microsoft.graph.downloadUrl")
    if not etag or not url:
        raise RuntimeError("Graph: %s sin eTag / downloadUrl" % filename)
    # downloadUrl viene pre-autenticada: sin header Authorization.
    with urllib.request.urlopen(urllib.request.Request(url), timeout=120) as r:
        return json.loads(r.read().decode("utf-8")), etag


_RE_ANIO_COHORTE = re.compile(r"^20\d{2}$")


def _sp_comex_root_id(token, site_id):
    """Id de la carpeta raiz /COMEX del sitio, o None si no existe. Espejo de solo-lectura de
    spComexRoot(create=False) en index.html -- nunca crea la carpeta."""
    try:
        meta = graph_request(token, "/sites/%s/drive/root:/COMEX" % site_id)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return meta.get("id")


def _match_embarque_folder(items, embarque_id):
    """Misma regla que spEmbItem: el nombre es el id, o empieza con el id y el caracter siguiente
    no es un digito (para no matchear LUMI_30 contra LUMI_304)."""
    for it in items:
        if not it.get("folder"):
            continue
        n = it.get("name") or ""
        if n == embarque_id or (
            n.startswith(embarque_id)
            and not (len(n) > len(embarque_id) and n[len(embarque_id)].isdigit())
        ):
            return it
    return None


def resolver_carpeta_sp(token, site_id, embarque_id):
    """Espejo de solo-lectura de spComexRoot(create=False) + spEmbItem(id, create=False) de
    index.html: busca la carpeta del embarque dentro de /COMEX (directo, o adentro de una
    subcarpeta de cohorte tipo "2024") y devuelve su webUrl, o None si no la encuentra. No crea
    nada nunca (a diferencia de la herramienta, que puede crear la carpeta si no existe)."""
    root_id = _sp_comex_root_id(token, site_id)
    if not root_id:
        return None
    kids = (graph_request(token, "/sites/%s/drive/items/%s/children?$top=400" % (site_id, root_id))
            .get("value") or [])
    folder = _match_embarque_folder(kids, embarque_id)
    if not folder:
        for y in kids:
            if y.get("folder") and _RE_ANIO_COHORTE.match(y.get("name") or ""):
                ykids = (graph_request(token, "/sites/%s/drive/items/%s/children?$top=400"
                                        % (site_id, y["id"])).get("value") or [])
                folder = _match_embarque_folder(ykids, embarque_id)
                if folder:
                    break
    if not folder:
        return None
    meta = graph_request(token, "/sites/%s/drive/items/%s?$select=webUrl" % (site_id, folder["id"]))
    return meta.get("webUrl")


def write_to_sharepoint(payload):
    token = graph_token()
    site_id = graph_site_id(token)
    graph_put_file(token, site_id, OUTPUT_FILE, json.dumps(payload).encode("utf-8"))
    return token, site_id


def sync_tablero_odoo(uid, token, site_id, embarques):
    """Upsert tracker -> x.comex.embarque + writeback de campos de mano (comex_odoo_sync.sincronizar)."""

    def odoo(model, method, args, kwargs=None):
        return odoo_execute_kw(uid, model, method, args, kwargs)

    def leer_tracker():
        return graph_get_json_con_etag(token, site_id, TRACKER_FILE)

    def escribir_tracker(data, etag):
        # Compacto y sin escapar unicode, como JSON.stringify de la herramienta.
        graph_put_file(
            token, site_id, TRACKER_FILE,
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), if_match=etag,
        )

    def guardar_backup(data):
        graph_put_file(
            token, site_id, TRACKER_BACKUP_FILE,
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def resolver_carpeta(embarque_id):
        return resolver_carpeta_sp(token, site_id, embarque_id)

    return comex_odoo_sync.sincronizar(
        odoo, leer_tracker, escribir_tracker, guardar_backup, embarques,
        dry_run=SYNC_DRY_RUN, resolver_carpeta=resolver_carpeta,
    )


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
    con_nac_real = sum(1 for e in embarques if e["nacReal"] is not None)
    print(
        "Resumen: %d embarques, %d parciales, %d sin TC resoluble, %d con nacReal"
        % (len(embarques), parciales, sin_tc, con_nac_real)
    )
    token, site_id = write_to_sharepoint(payload)
    print("OK: %s escrito en SharePoint (%s)" % (OUTPUT_FILE, SP_SITE))

    # El sync del tablero Odoo es un paso aparte: si falla, el overlay ya quedo escrito y el job
    # termina en rojo para que se vea, sin haber perdido lo anterior.
    try:
        resumen = sync_tablero_odoo(uid, token, site_id, embarques)
    except Exception as e:
        print("ERROR: sync del tablero Odoo: %s" % e, file=sys.stderr)
        return 1
    if resumen.get("errores"):
        print("ERROR: sync del tablero Odoo con errores: %s" % resumen["errores"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
