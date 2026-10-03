#!/usr/bin/env python3
"""
Sincronizacion bidireccional tracker <-> Odoo de los embarques COMEX de Argentina (LUMI_).

Se invoca desde scripts/comex_reales.py, DESPUES de escribir comex_odoo_real.json. No lee variables
de entorno ni habla con la red por su cuenta: todo lo externo entra por funciones inyectadas, asi que
la logica se prueba sin Odoo ni SharePoint (tests/test_comex_odoo_sync.py).

Sync por DUEÑO DE CAMPO (nunca last-write-wins por registro):

* Campos del pipeline (estado, fechas, contenedores, docs, costos, gastos y nacionalizacion): siempre
  tracker -> Odoo. En Odoo son solo lectura.
* Campos de mano (notas, despa, opDesp  <->  x_notas, x_despachante, x_operador): se editan en Odoo.
  Odoo guarda el ultimo valor sincronizado en x_*_sync; si el valor actual de Odoo difiere de ese
  snapshot, el usuario lo edito en Odoo y ESE valor gana y se escribe de vuelta en comex_data.json.
  Si no cambio en Odoo, manda el tracker.

Fase A -- leer Odoo, resolver los campos de mano y escribir en comex_data.json SOLO esos campos.
Fase B -- upsert tracker -> Odoo (por name).

Seguridad del tracker: la escritura de comex_data.json se hace con If-Match (etag) y se aborta si el
diff contra lo leido toca algo que no sea (a) los campos de mano cambiados en Odoo, (b) el `_m` de
esos registros y (c) el `_ts` global. `_m` y `_ts` los usa la herramienta (index.html, mergeCloudInto
y cloudPush) para detectar que la nube cambio y fusionar sin pisar: si no se actualizaran, una
sesion abierta de la herramienta podria sobreescribir el cambio. Despues de escribir se vuelve a leer
y se verifica de nuevo.
"""
import copy
import json
import re
import time
import unicodedata
from datetime import date, datetime, timezone

MODELO = "x.comex.embarque"
COMPANY_ID = 6
PREFIJO_AR = "LUMI_"

ESTADOS = [
    "Orden de compra", "En producción", "Embarcado", "En tránsito",
    "Arribado", "En despacho", "Nacionalizado", "Entregado",
]

# (campo Odoo, clave del tracker)
CAMPOS_MANO = (
    ("x_notas", "notas"),
    ("x_despachante", "despa"),
    ("x_operador", "opDesp"),
)

# Defaults de gastosOpEst de la herramienta (index.html, gastosOpDefaults): la herramienta los
# lee de S.params (gp_despPct, gp_termCont, gp_fwd, gp_fijos) y cae a estos valores.
GASTOS_DEFAULTS = {"gp_despPct": 0.005, "gp_termCont": 2000.0, "gp_fwd": 930.0, "gp_fijos": 200.0}

# Mirror manual de DOC_CAT (index.html) -- (tipo, nombre, etapa_min). Solo Argentina: el sync de
# x.comex.embarque es LUMI_* unicamente (ver PREFIJO_AR); si algun dia se sincroniza Peru, agregar
# el equivalente de DOC_CAT_PE aca.
DOC_CAT_AR = (
    ("orden", "Orden / Reserva", 0),
    ("proforma", "Factura proforma", 1),
    ("fcprov", "Factura comercial del proveedor", 2),
    ("swift", "Swift / comprobante de pago a proveedor", 2),
    ("packing", "Packing list", 2),
    ("bl", "BL / HBL", 2),
    ("fflete", "Factura de flete / agente de carga", 3),
    ("aviso", "Aviso de llegada", 4),
    ("despacho", "Despacho / DI (provisorio)", 5),
    ("fterminal", "Factura de terminal", 5),
    ("gastos", "Gastos de agente / honorarios", 5),
    ("dioficial", "DI oficializada", 6),
    ("boletas", "Boletas de pago de impuestos", 6),
    ("liquid", "Liquidación final del despachante", 6),
)


class ConflictoTracker(Exception):
    """El etag de comex_data.json cambio entre la lectura y la escritura (HTTP 412)."""


# ----------------------------------------------------------------------------- helpers puros

def _sin_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def texto(v):
    """None / False -> ''. El resto, tal cual (sin strip: las notas se preservan)."""
    if v is None or v is False:
        return ""
    return v if isinstance(v, str) else str(v)


_NUM_RE = re.compile(r"\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def num(v):
    """Igual que num() de index.html (parseFloat o 0)."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.match(v) if isinstance(v, str) else None
    return float(m.group(0)) if m else 0.0


def fecha_odoo(v):
    """'YYYY-MM-DD' valido -> el mismo string; cualquier otra cosa -> False."""
    s = texto(v)[:10]
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return False


def estado_odoo(v):
    """Mapea el estado del tracker al selection de Odoo (sin acentos / mayusculas); False si no matchea."""
    k = _sin_acentos(texto(v).strip().lower())
    for e in ESTADOS:
        if _sin_acentos(e.lower()) == k:
            return e
    return False


def operador_a_odoo(v):
    """opDesp del tracker (texto libre) -> 'MC' / 'TR' / 'OT' / ''."""
    k = _sin_acentos(texto(v).strip().lower())
    if not k:
        return ""
    if "mundo" in k:
        return "MC"
    if k.startswith("trice"):
        return "TR"
    return "OT"


def operador_a_tracker(sel):
    return {"MC": "Mundo Comex", "TR": "Trice", "OT": "Otro"}.get(texto(sel), "")


def ahora_odoo(ahora=None):
    return (ahora or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------- Fase A

def resolver_mano(odoo_rec, seg):
    """Resuelve los 3 campos de mano de UN embarque.

    Devuelve {clave_tracker: {...}} con, por campo:
      gana          'odoo' | 'tracker'
      odoo          valor final en representacion de Odoo (para x_campo y x_campo_sync)
      tracker       valor final en representacion del tracker
      cambia_tracker True si hay que escribir `tracker` en comex_data.json
    """
    out = {}
    for xf, tk in CAMPOS_MANO:
        v_odoo = texto(odoo_rec.get(xf)) if odoo_rec else ""
        v_sync = texto(odoo_rec.get(xf + "_sync")) if odoo_rec else ""
        v_trk = texto(seg.get(tk))
        es_op = xf == "x_operador"
        trk_en_odoo = operador_a_odoo(v_trk) if es_op else v_trk
        if odoo_rec and v_odoo != v_sync:
            out[tk] = {
                "campo": xf, "gana": "odoo", "odoo": v_odoo,
                "tracker": operador_a_tracker(v_odoo) if es_op else v_odoo,
                "cambia_tracker": trk_en_odoo != v_odoo,
            }
        else:
            out[tk] = {
                "campo": xf, "gana": "tracker", "odoo": trk_en_odoo, "tracker": v_trk,
                "cambia_tracker": False,
            }
    return out


def cambios_para_tracker(segs_ar, odoo_por_id):
    """{id: {clave_tracker: valor}} con lo que hay que escribir en comex_data.json."""
    cambios = {}
    for seg in segs_ar:
        rec = odoo_por_id.get(seg.get("id"))
        if not rec:
            continue
        for tk, r in resolver_mano(rec, seg).items():
            if r["gana"] == "odoo" and r["cambia_tracker"]:
                cambios.setdefault(seg["id"], {})[tk] = r["tracker"]
    return cambios


def aplicar_cambios_tracker(data, cambios, ahora_ms):
    """Copia de `data` con los campos de mano cambiados, `_m` de esos registros y `_ts` global."""
    nuevo = copy.deepcopy(data)
    ts = max(int(ahora_ms), int(data.get("_ts") or 0) + 1)
    for seg in nuevo.get("seg") or []:
        ch = cambios.get(seg.get("id")) if isinstance(seg, dict) else None
        if ch:
            seg.update(ch)
            seg["_m"] = ts
    nuevo["_ts"] = ts
    return nuevo


def verificar_diff(antes, despues, cambios):
    """Lista de problemas (vacia = OK). Solo se permite que cambien: los campos de `cambios`, el `_m`
    de esos registros y el `_ts` global. Cualquier otra diferencia es un problema."""
    problemas = []
    for k in set(antes) | set(despues):
        if k in ("_ts", "seg"):
            continue
        if antes.get(k) != despues.get(k):
            problemas.append("cambio el campo global %r" % k)
    if cambios and (despues.get("_ts") or 0) <= (antes.get("_ts") or 0):
        problemas.append("_ts no avanzo")
    sa, sd = antes.get("seg") or [], despues.get("seg") or []
    if len(sa) != len(sd):
        problemas.append("cambio la cantidad de embarques (%d -> %d)" % (len(sa), len(sd)))
        return problemas
    for ra, rd in zip(sa, sd):
        ident = ra.get("id") if isinstance(ra, dict) else None
        if not isinstance(ra, dict) or not isinstance(rd, dict) or ident != rd.get("id"):
            problemas.append("cambio el orden o la identidad de los embarques (%r)" % ident)
            continue
        ch = cambios.get(ident)
        for k in set(ra) | set(rd):
            if ch and k == "_m":
                if rd.get("_m") is None:
                    problemas.append("%s: falta _m" % ident)
                continue
            if ch and k in ch:
                if rd.get(k) != ch[k]:
                    problemas.append("%s.%s no quedo con el valor esperado" % (ident, k))
                continue
            if ra.get(k, _FALTA) != rd.get(k, _FALTA):
                problemas.append("%s.%s cambio y no debia" % (ident, k))
    return problemas


_FALTA = object()


# ----------------------------------------------------------------------------- Fase B

def gastos_params(data):
    """Parametros de gastosOpEst: S.params del tracker si estan, si no los defaults de la herramienta."""
    p = (data or {}).get("params") or {}
    out = {}
    for k, default in GASTOS_DEFAULTS.items():
        v = p.get(k)
        out[k] = default if v is None or v == "" else num(v)
    return out


def va_embarque(seg, base_ncm):
    """Valor en aduana (base CIF) estimado de un embarque, igual que computeNac de la herramienta:
    `nacVA` si esta cargado; si no, la suma de FOB de `ncmMix` x (1 + flete% + seguro%) con los
    porcentajes de S.ncm.base. None si no se puede calcular (VA pendiente)."""
    va = num(seg.get("nacVA"))
    if va:
        return va
    sum_fob = sum(num(m.get("fob")) for m in (seg.get("ncmMix") or []) if isinstance(m, dict))
    if not sum_fob:
        return None
    b = base_ncm or {}
    return sum_fob * (1 + (num(b.get("fletePctDefault")) + num(b.get("seguroPct"))) / 100.0)


def gastos_est_breakdown(va, contenedores, gp=None):
    """Desglose de gastosOpEst (index.html: gastosOpEst) -- 4 terminos fijos, no es el motor de
    calculo (eso es computeNac, que no se reimplementa): honorario del despachante (% del VA) +
    terminal por contenedor + forwarder (Trice) fijo por embarque + operativos fijos. Sin VA el
    honorario queda en 0 (VA pendiente) pero el resto de los terminos se suman igual. Fase 2.2:
    cada termino se espeja por separado al panel "Gastos operativos de despacho"."""
    gp = gp or GASTOS_DEFAULTS
    honor = (va or 0.0) * gp["gp_despPct"]
    term = contenedores * gp["gp_termCont"]
    fwd = gp["gp_fwd"]
    fijos = gp["gp_fijos"]
    return {"honor": honor, "term": term, "fwd": fwd, "fijos": fijos, "total": honor + term + fwd + fijos}


def gastos_est_usd(va, contenedores, gp=None):
    """Total de gastosOpEst. Ver gastos_est_breakdown() para el desglose por concepto."""
    return gastos_est_breakdown(va, contenedores, gp)["total"]


def saldo_pagos(seg):
    return sum(num(p.get("monto")) for p in (seg.get("pagos") or [])
               if isinstance(p, dict) and not p.get("pagado"))


def comandos_docs(seg):
    """Comandos One2many para x_doc_ids a partir de docsChk (dict {tipo: True|'na'} del tracker).

    Reemplaza el set completo (unlink-all + create-all) en vez de diffear fila a fila: mas simple,
    y sigue siendo idempotente -- nunca duplica, converge siempre al mismo estado final -- porque
    estas filas no tienen ninguna referencia externa que preservar entre corridas.
    """
    chk = seg.get("docsChk") or {}
    comandos = [(5, 0, 0)]
    for i, (tipo, nombre, etapa_min) in enumerate(DOC_CAT_AR):
        v = chk.get(tipo) if isinstance(chk, dict) else None
        estado = "ok" if v is True else ("na" if v == "na" else "pend")
        comandos.append((0, 0, {
            "tipo": tipo, "nombre": nombre, "etapa_min": etapa_min, "estado": estado,
            "sequence": (i + 1) * 10,
        }))
    return comandos


def valores_pipeline(seg, real, va, gp=None, ahora=None):
    """Campos que mandan SIEMPRE tracker -> Odoo (mas lo calculado a partir de comex_odoo_real.json)."""
    real = real or {}
    gr = real.get("gastosReal") or {}
    nr = real.get("nacReal") or {}
    snap = real.get("nacEstSnap")
    if snap is None:
        snap = seg.get("nacEstSnap")
    nac_est = num(snap) if snap is not None and snap != "" else num(seg.get("nacEst"))
    conts = int(num(seg.get("conts")))
    gastos = gastos_est_breakdown(va, conts, gp)
    return {
        "name": seg["id"],
        "company_id": COMPANY_ID,
        "x_pais": "AR",
        "x_prov": texto(seg.get("prov")),
        "x_prod": texto(seg.get("prod")),
        "x_origen": texto(seg.get("origen")),
        "x_modo": texto(seg.get("modo")),
        "x_incoterm": texto(seg.get("inco")),
        "x_estado": estado_odoo(seg.get("estado")),
        "x_f_orden": fecha_odoo(seg.get("fOrden")),
        "x_etd": fecha_odoo(seg.get("etd")),
        "x_eta": fecha_odoo(seg.get("eta")),
        "x_f_ofic": fecha_odoo(seg.get("fOfic")),
        "x_f_lib": fecha_odoo(seg.get("fLib")),
        "x_contenedores": conts,
        "x_docs_pend": texto(seg.get("docs")),
        "x_costo_est": num(seg.get("costEst")),
        "x_nac_est": nac_est,
        "x_nac_real": num(nr.get("desembolso")) if nr else 0.0,
        # Fase 2.1 -- desglose del despacho real (nacReal ya lo trae completo desde
        # build_nac_real; el estimado solo tiene agregados -- ver nota en el modulo Odoo).
        "x_nac_real_va": num(nr.get("VA")) if nr else 0.0,
        "x_nac_real_norecup": num(nr.get("noRecup")) if nr else 0.0,
        "x_nac_real_iva": num(nr.get("iva")) if nr else 0.0,
        "x_nac_real_piva": num(nr.get("pIva")) if nr else 0.0,
        "x_nac_real_pgan": num(nr.get("pGan")) if nr else 0.0,
        "x_nac_real_impint": num(nr.get("impInt")) if nr else 0.0,
        "x_nac_real_iibb": num(nr.get("iibb")) if nr else 0.0,
        "x_nac_real_credito": num(nr.get("credito")) if nr else 0.0,
        "x_nac_real_tc": num(nr.get("tc")) if nr else 0.0,
        "x_nac_real_di": texto(nr.get("di")) if nr else "",
        "x_nac_real_fecha": fecha_odoo(nr.get("fecha")) if nr else False,
        "x_gastos_est": gastos["total"],
        # Fase 2.2 -- desglose del estimado por concepto (misma formula ya aprobada, solo se
        # exponen los 4 terminos en vez de solo el total).
        "x_gastos_honor": gastos["honor"],
        "x_gastos_term": gastos["term"],
        "x_gastos_fwd": gastos["fwd"],
        "x_gastos_fijos": gastos["fijos"],
        "x_gastos_real": num(gr.get("total")) if gr else 0.0,
        "x_parcial": bool(gr.get("parcial")) if gr else False,
        "x_saldo_pagos": saldo_pagos(seg),
        "x_sync_ts": ahora_odoo(ahora),
    }


def valores_mano(resueltos, omitir):
    """Campos de mano + snapshot `_sync` a escribir en Odoo. `omitir`: claves del tracker que no se tocan."""
    vals = {}
    for tk, r in resueltos.items():
        if tk in omitir:
            continue
        # x_operador es un Selection: el vacio canonico es False (el ORM tambien tolera '', verificado).
        vals[r["campo"]] = (r["odoo"] or False) if r["campo"] == "x_operador" else r["odoo"]
        vals[r["campo"] + "_sync"] = r["odoo"]
    return vals


# ----------------------------------------------------------------------------- orquestacion

# Campos que agrega la Fase 2.1+2.2: si el sync llega antes de que Odoo.sh instale/actualice el
# modulo con estos campos (o al reves), hay que omitirlos en vez de romper -- ver
# _soporta_nac_gastos_breakdown().
CAMPOS_NAC_GASTOS_BREAKDOWN = (
    "x_nac_real_va", "x_nac_real_norecup", "x_nac_real_iva", "x_nac_real_piva", "x_nac_real_pgan",
    "x_nac_real_impint", "x_nac_real_iibb", "x_nac_real_credito", "x_nac_real_tc", "x_nac_real_di",
    "x_nac_real_fecha", "x_gastos_honor", "x_gastos_term", "x_gastos_fwd", "x_gastos_fijos",
)


def _soporta_nac_gastos_breakdown(odoo):
    """True si esta instancia ya tiene los campos del desglose de nacReal real / gastos estimados
    (Fase 2.1+2.2) en x.comex.embarque. Mismo patron de _soporta_docs (ver mas abajo): si el modulo
    Odoo y este sync se despliegan en momentos distintos, el sync sigue escribiendo el resto de los
    campos sin este desglose en vez de romper -- ya paso 3 veces con deploys desfasados entre estos
    dos repos (RNG invalido, editable="false", y el guard de docsChk que esto imita)."""
    campos = odoo(MODELO, "fields_get", [], {"attributes": []})
    return all(c in campos for c in CAMPOS_NAC_GASTOS_BREAKDOWN)


def _soporta_docs(odoo):
    """True si esta instancia ya tiene el checklist de documentos / carpeta SharePoint de la Fase 1
    del tablero: el modelo x.comex.doc Y los campos x_sp_folder_url / x_doc_ids en x.comex.embarque.
    Deploy en dos repos (modulo Odoo + este sync): si uno llega antes que el otro -- el modulo se
    mergeo pero Odoo.sh todavia no lo actualizo, o este sync se mergeo antes que el modulo --, esto
    da False y el sync sigue sin esos campos en vez de romper (mismo criterio que el chequeo de
    modulo instalado de mas abajo, pero sin saltear el resto del sync)."""
    if not odoo("ir.model", "search_count", [[["model", "=", "x.comex.doc"]]]):
        return False
    campos = odoo(MODELO, "fields_get", [], {"attributes": []})
    return "x_sp_folder_url" in campos and "x_doc_ids" in campos


def sincronizar(odoo, leer_tracker, escribir_tracker, guardar_backup, reales,
                dry_run=False, log=print, ahora=None, max_intentos=3, resolver_carpeta=None):
    """odoo(model, method, args, kwargs=None); leer_tracker() -> (data, etag);
    escribir_tracker(data, etag) (lanza ConflictoTracker ante 412); guardar_backup(data);
    reales = lista `embarques` de comex_odoo_real.json. resolver_carpeta(embarque_id) -> url|None,
    opcional: se llama como mucho una vez por embarque (solo si todavia no tiene x_sp_folder_url) y
    nunca bloquea el resto del sync si falla. Devuelve un resumen (dict)."""
    ctx = {"context": {"allowed_company_ids": [COMPANY_ID]}}
    resumen = {"skipped": False, "tracker_escrito": False, "writeback_ok": True,
               "cambios_tracker": {}, "creados": 0, "actualizados": 0, "va_pendiente": [],
               "errores": []}

    if not odoo("ir.model", "search_count", [[["model", "=", MODELO]]]):
        log("SYNC: el modelo %s no existe en Odoo (modulo comex_dashboard sin instalar); se omite." % MODELO)
        resumen["skipped"] = True
        return resumen

    soporta_docs = _soporta_docs(odoo)
    if not soporta_docs:
        log("SYNC: el checklist de documentos (x.comex.doc / x_sp_folder_url / x_doc_ids) todavia no "
            "esta instalado/actualizado en esta instancia; se omite el espejo de documentos y de "
            "carpeta SharePoint, el resto del sync sigue igual.")
    soporta_nac_gastos = _soporta_nac_gastos_breakdown(odoo)
    if not soporta_nac_gastos:
        log("SYNC: el desglose de nacionalizacion real / gastos estimados (Fase 2.1+2.2) todavia no "
            "esta instalado/actualizado en esta instancia; se omite ese desglose, el resto del sync "
            "sigue igual.")

    campos_leer = ["name"] + (["x_sp_folder_url"] if soporta_docs else [])
    campos_leer += [f for xf, _tk in CAMPOS_MANO for f in (xf, xf + "_sync")]
    data, etag = leer_tracker()

    def segs_ar(d):
        return [s for s in (d.get("seg") or []) if isinstance(s, dict)
                and str(s.get("id") or "").startswith(PREFIJO_AR)]

    def leer_odoo(ids):
        recs = odoo(MODELO, "search_read", [[["name", "in", ids]]], dict(ctx, fields=campos_leer))
        return {r["name"]: r for r in recs}

    # ---- Fase A
    ids_ar = [s["id"] for s in segs_ar(data)]
    odoo_a = leer_odoo(ids_ar)
    cambios = cambios_para_tracker(segs_ar(data), odoo_a)
    escritura_fallo = set()
    intento = 0
    while cambios:
        intento += 1
        log("SYNC fase A: %d embarque(s) con campos de mano editados en Odoo: %s" % (
            len(cambios), ", ".join("%s%s" % (i, sorted(c)) for i, c in sorted(cambios.items()))))
        resumen["cambios_tracker"] = {i: dict(c) for i, c in cambios.items()}
        if dry_run:
            log("SYNC (dry-run): no se escribe comex_data.json.")
            escritura_fallo = set(cambios)
            break
        nuevo = aplicar_cambios_tracker(data, cambios, int(time.time() * 1000))
        problemas = verificar_diff(data, nuevo, cambios)
        if problemas:
            log("ERROR SYNC: el diff del tracker se desvia, NO se escribe: %s" % "; ".join(problemas[:10]))
            resumen["errores"].append("diff desviado")
            resumen["writeback_ok"] = False
            escritura_fallo = set(cambios)
            break
        try:
            guardar_backup(data)
            escribir_tracker(nuevo, etag)
        except ConflictoTracker:
            if intento >= max_intentos:
                log("ERROR SYNC: conflicto persistente al escribir comex_data.json (%d intentos); "
                    "los cambios quedan para la proxima corrida." % intento)
                resumen["errores"].append("conflicto persistente")
                resumen["writeback_ok"] = False
                escritura_fallo = set(cambios)
                break
            log("SYNC: comex_data.json cambio mientras lo procesaba, reintento (%d)." % intento)
            data, etag = leer_tracker()
            odoo_a = leer_odoo([s["id"] for s in segs_ar(data)])
            cambios = cambios_para_tracker(segs_ar(data), odoo_a)
            continue
        releido, _et = leer_tracker()
        post = verificar_diff(nuevo, releido, {})
        if post:
            log("ERROR SYNC: el tracker releido NO coincide con lo escrito: %s" % "; ".join(post[:10]))
            resumen["errores"].append("verificacion posterior")
            resumen["writeback_ok"] = False
        else:
            log("SYNC fase A: comex_data.json actualizado y verificado (solo %d registro(s) tocados)." % len(cambios))
        resumen["tracker_escrito"] = True
        data = releido
        break

    # ---- Fase B
    segs = segs_ar(data)
    gp = gastos_params(data)
    base_ncm = (data.get("ncm") or {}).get("base") or {}
    odoo_b = leer_odoo([s["id"] for s in segs])
    reales_por_id = {e.get("id"): e for e in reales or [] if isinstance(e, dict)}
    for seg in segs:
        ident = seg["id"]
        rec_a, rec_b = odoo_a.get(ident), odoo_b.get(ident)
        omitir = set()
        resueltos = resolver_mano(rec_a, seg)
        for tk, r in resueltos.items():
            # Si Odoo gano pero el tracker no se pudo actualizar, o si alguien edito ese campo en Odoo
            # entre la fase A y ahora: no se toca (ni valor ni snapshot) y queda para la proxima corrida.
            if r["gana"] == "odoo" and ident in escritura_fallo:
                omitir.add(tk)
            elif rec_a and rec_b and texto(rec_b.get(r["campo"])) != texto(rec_a.get(r["campo"])):
                omitir.add(tk)
        va = va_embarque(seg, base_ncm)
        if va is None:
            resumen["va_pendiente"].append(ident)
            log("SYNC: %s VA pendiente (sin nacVA ni ncmMix); x_gastos_est sin honorario, solo terminos fijos." % ident)
        vals = valores_pipeline(seg, reales_por_id.get(ident), va, gp, ahora)
        if not soporta_nac_gastos:
            for k in CAMPOS_NAC_GASTOS_BREAKDOWN:
                vals.pop(k, None)
        vals.update(valores_mano(resueltos, omitir))
        if soporta_docs:
            vals["x_doc_ids"] = comandos_docs(seg)
            if resolver_carpeta and not (rec_b and rec_b.get("x_sp_folder_url")):
                try:
                    url = resolver_carpeta(ident)
                except Exception as e:
                    url = None
                    log("SYNC: no se pudo resolver la carpeta de SharePoint de %s: %s" % (ident, e))
                if url:
                    vals["x_sp_folder_url"] = url
        if rec_b:
            # no reescribir lo que ya esta igual en los campos de mano / snapshot
            for k in [f for xf, _t in CAMPOS_MANO for f in (xf, xf + "_sync")]:
                if k in vals and texto(rec_b.get(k)) == texto(vals[k]):
                    del vals[k]
        if dry_run:
            log("SYNC (dry-run): %s -> %s %s" % (ident, "write" if rec_b else "create",
                                                  json.dumps(vals, ensure_ascii=False, default=str)))
            continue
        if rec_b:
            vals.pop("name", None)
            odoo(MODELO, "write", [[rec_b["id"]], vals], ctx)
            resumen["actualizados"] += 1
        else:
            odoo(MODELO, "create", [vals], ctx)
            resumen["creados"] += 1
    log("SYNC fase B: %d creado(s), %d actualizado(s), %d con VA pendiente." % (
        resumen["creados"], resumen["actualizados"], len(resumen["va_pendiente"])))
    return resumen
