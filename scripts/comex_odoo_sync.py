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

# Parametros de gastosOpEst de la herramienta (index.html, gastosOpDefaults).
GASTOS_PCT_CIF = 0.005
GASTOS_USD_POR_CONTENEDOR = 2000.0
GASTOS_USD_FORWARDER = 930.0
GASTOS_USD_FIJOS = 200.0

PETDUR_PATTERN = "petdur%"


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

def gastos_est_usd(cif_usd, contenedores):
    """gastosOpEst de la herramienta: 0,5% del CIF + USD 2.000 x contenedores + 930 forwarder + 200 fijos.
    Sin CIF no se estima (0)."""
    if not cif_usd:
        return 0.0
    return (cif_usd * GASTOS_PCT_CIF + contenedores * GASTOS_USD_POR_CONTENEDOR
            + GASTOS_USD_FORWARDER + GASTOS_USD_FIJOS)


def saldo_pagos(seg):
    return sum(num(p.get("monto")) for p in (seg.get("pagos") or [])
               if isinstance(p, dict) and not p.get("pagado"))


def cif_petdur_usd(odoo, cohorte, normalizar_cohorte):
    """CIF del embarque a partir de las facturas de Petdur (USD, sin impuestos) cuya referencia
    pertenece a la cohorte. None si no hay ninguna."""
    movs = odoo(
        "account.move", "search_read",
        [[["partner_id.name", "=ilike", PETDUR_PATTERN],
          ["move_type", "in", ["in_invoice", "out_invoice"]], ["state", "=", "posted"],
          ["ref", "ilike", cohorte[-3:]]]],
        {"fields": ["ref", "amount_untaxed", "currency_id", "move_type"],
         "context": {"allowed_company_ids": [COMPANY_ID]}, "limit": 50})
    total, hay = 0.0, False
    for m in movs:
        if normalizar_cohorte(m.get("ref")) != cohorte:
            continue
        if (m.get("currency_id") or [None, ""])[1] != "USD":
            continue
        total += m.get("amount_untaxed") or 0.0
        hay = True
    return total if hay else None


def valores_pipeline(seg, real, cif_usd, ahora=None):
    """Campos que mandan SIEMPRE tracker -> Odoo (mas lo calculado a partir de comex_odoo_real.json)."""
    real = real or {}
    gr = real.get("gastosReal") or {}
    nr = real.get("nacReal") or {}
    snap = real.get("nacEstSnap")
    if snap is None:
        snap = seg.get("nacEstSnap")
    nac_est = num(snap) if snap is not None and snap != "" else num(seg.get("nacEst"))
    conts = int(num(seg.get("conts")))
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
        "x_gastos_est": gastos_est_usd(cif_usd, conts),
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
        vals[r["campo"]] = r["odoo"]
        vals[r["campo"] + "_sync"] = r["odoo"]
    return vals


# ----------------------------------------------------------------------------- orquestacion

def sincronizar(odoo, leer_tracker, escribir_tracker, guardar_backup, reales, normalizar_cohorte,
                dry_run=False, log=print, ahora=None, max_intentos=3):
    """odoo(model, method, args, kwargs=None); leer_tracker() -> (data, etag);
    escribir_tracker(data, etag) (lanza ConflictoTracker ante 412); guardar_backup(data);
    reales = lista `embarques` de comex_odoo_real.json. Devuelve un resumen (dict)."""
    ctx = {"context": {"allowed_company_ids": [COMPANY_ID]}}
    resumen = {"skipped": False, "tracker_escrito": False, "writeback_ok": True,
               "cambios_tracker": {}, "creados": 0, "actualizados": 0, "cif_pendiente": [],
               "errores": []}

    if not odoo("ir.model", "search_count", [[["model", "=", MODELO]]]):
        log("SYNC: el modelo %s no existe en Odoo (modulo comex_dashboard sin instalar); se omite." % MODELO)
        resumen["skipped"] = True
        return resumen

    campos_leer = ["name"] + [f for xf, _tk in CAMPOS_MANO for f in (xf, xf + "_sync")]
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
        cif = cif_petdur_usd(odoo, ident, normalizar_cohorte)
        if cif is None:
            resumen["cif_pendiente"].append(ident)
            log("SYNC: %s CIF pendiente (sin factura de Petdur para la cohorte); x_gastos_est = 0." % ident)
        vals = valores_pipeline(seg, reales_por_id.get(ident), cif, ahora)
        vals.update(valores_mano(resueltos, omitir))
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
    log("SYNC fase B: %d creado(s), %d actualizado(s), %d con CIF pendiente." % (
        resumen["creados"], resumen["actualizados"], len(resumen["cif_pendiente"])))
    return resumen
