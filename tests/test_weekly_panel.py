"""Tests del panel de cumplimiento semanal (quiosco-2jp.3)."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

from quiosco import weekly_panel

AHORA = datetime(2026, 9, 2, 15, 30, 0, tzinfo=timezone.utc)


def _bloque(closed=10, ttf=(8, 2), ttr=(7, 3), no_deadline=1,
            desde="2026-09-02T12:30:00Z", hasta="2026-09-09T12:30:00Z"):
    return {
        "from": desde, "to": hasta, "closed": closed,
        "ttf": {"ok": ttf[0], "bad": ttf[1]},
        "ttr": {"ok": ttr[0], "bad": ttr[1]},
        "no_deadline": no_deadline,
    }


def _persona(pid, nombre, *, actual=None, anterior=None, hasta_aqui=None):
    return {
        "id": pid, "name": nombre,
        "current": actual or _bloque(),
        "previous": anterior or _bloque(),
        "previous_to_date": hasta_aqui or _bloque(),
    }


def _payload(*, actual=None, anterior=None, hasta_aqui=None, personas=None):
    cur = actual or _bloque()
    cur = {**cur, "by_person": personas if personas is not None else []}
    return {
        "generated_at": "2026-09-02T15:17:55Z",
        "current": cur,
        "previous": anterior or _bloque(
            closed=107, ttf=(76, 9), ttr=(71, 32), no_deadline=22,
            desde="2026-08-26T12:30:00Z", hasta="2026-09-02T12:30:00Z"),
        # El flow lo emite calculado y con SIETE decimales de segundo.
        "previous_to_date": hasta_aqui or _bloque(
            closed=11, ttf=(10, 0), ttr=(9, 2), no_deadline=1,
            desde="2026-08-26T12:30:00Z", hasta="2026-08-26T15:18:02.0000000Z"),
    }


class PorcentajeTest(unittest.TestCase):
    def test_sin_masa_no_hay_porcentaje(self):
        # Un 50 % sobre n=2 es ruido con aires de metrica.
        self.assertIsNone(weekly_panel.pct(1, 2, min_n=8))

    def test_con_masa_redondea(self):
        self.assertEqual(weekly_panel.pct(7, 9, min_n=8), 78)

    def test_total_cero_no_divide(self):
        self.assertIsNone(weekly_panel.pct(0, 0, min_n=1))

    def test_los_sin_plazo_no_entran_al_denominador(self):
        # Meter una ausencia de dato en el denominador hundiria el ratio.
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=30, ttf=(8, 2), ttr=(7, 3), no_deadline=20)),
            now=AHORA, min_n=8)
        self.assertEqual(v["current"]["ttf"]["total"], 10)
        self.assertEqual(v["current"]["ttf"]["pct"], 80)
        self.assertEqual(v["current"]["no_deadline"], 20)


class SemanaVerdeTest(unittest.TestCase):
    """El caso del miercoles a media mañana, que es cuando el panel se estrena."""

    def _vista_verde(self):
        return weekly_panel.build_view(
            _payload(actual=_bloque(closed=0, ttf=(0, 0), ttr=(0, 0), no_deadline=0),
                     personas=[_persona(8, "Puchuri, Jacqueline",
                                        actual=_bloque(0, (0, 0), (0, 0), 0),
                                        anterior=_bloque(17, (5, 1), (5, 9), 11))]),
            now=AHORA, min_n=8)

    def test_la_semana_recien_abierta_se_declara(self):
        v = self._vista_verde()
        self.assertTrue(v["fresh_week"])
        self.assertEqual(v["headline"], "La semana acaba de empezar")
        # 0 contra 11 se lee como arranque; 0 contra 103, como catastrofe.
        self.assertIn("11", v["subline"])

    def test_con_la_semana_verde_se_muestra_la_anterior_rotulada(self):
        # Una tabla de ceros no informa; la semana cerrada si, con su rotulo.
        v = self._vista_verde()
        self.assertEqual(v["mode"], "previous")
        self.assertEqual(v["table_label"], "semana anterior, ya cerrada")
        self.assertEqual(v["rows"][0]["closed"], 17)
        self.assertEqual(v["rows"][0]["ttr"], {"ok": 5, "bad": 9, "total": 14, "pct": 36})

    def test_con_la_semana_avanzada_se_muestra_la_actual(self):
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)),
                     personas=[_persona(8, "Puchuri, Jacqueline")]),
            now=AHORA, min_n=8)
        self.assertFalse(v["fresh_week"])
        self.assertEqual(v["mode"], "current")
        self.assertEqual(v["table_label"], "semana en curso")
        self.assertEqual(v["headline"], "40 cerrados esta semana")


class FilasTest(unittest.TestCase):
    def test_ordena_por_volumen_cerrado_no_por_cumplimiento(self):
        # Ordenar por porcentaje convierte un sesgo conocido en un ranking, y
        # el ultimo suele ser quien vacio backlog (bead quiosco-rfv).
        personas = [
            _persona(1, "Poco, Cierra", actual=_bloque(3, (3, 0), (3, 0))),
            _persona(2, "Mucho, Cierra", actual=_bloque(15, (9, 6), (3, 12))),
        ]
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=18, ttf=(12, 6), ttr=(6, 12)),
                     personas=personas),
            now=AHORA, min_n=8)
        self.assertEqual([f["name"] for f in v["rows"]],
                         ["Cierra Mucho", "Cierra Poco"])

    def test_apellido_coma_nombre_se_voltea_y_deja_iniciales(self):
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)),
                     personas=[_persona(19, "Rodriguez, Saul")]),
            now=AHORA, min_n=8)
        self.assertEqual(v["rows"][0]["name"], "Saul Rodriguez")
        self.assertEqual(v["rows"][0]["initials"], "SR")
        self.assertEqual(v["rows"][0]["photo"], "")

    def test_la_foto_sale_de_disco_cuando_existe(self):
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)),
                     personas=[_persona(19, "Rodriguez, Saul")]),
            now=AHORA, min_n=8, photo_ids=frozenset({"19"}))
        self.assertEqual(v["rows"][0]["photo"], "/static/photos/19.jpg")

    def test_el_payload_no_puede_inyectar_una_foto(self):
        p = _persona(19, "Rodriguez, Saul")
        p["photo"] = "https://malo.example/x.jpg"
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)), personas=[p]),
            now=AHORA, min_n=8)
        self.assertEqual(v["rows"][0]["photo"], "")

    def test_show_people_apagado_deja_la_tabla_sin_personas(self):
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)),
                     personas=[_persona(19, "Rodriguez, Saul")]),
            now=AHORA, min_n=8, show_people=False)
        self.assertEqual(v["rows"], [])

    def test_tope_de_filas_y_omitidos(self):
        personas = [_persona(i, f"Apellido{i}, Nombre",
                             actual=_bloque(20 - i, (8, 2), (7, 3)))
                    for i in range(1, 6)]
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=90, ttf=(40, 10), ttr=(35, 15)),
                     personas=personas),
            now=AHORA, min_n=8, max_rows=3)
        self.assertEqual(len(v["rows"]), 3)
        self.assertEqual(v["omitted"], 2)

    def test_una_persona_basura_no_revienta_la_tabla(self):
        v = weekly_panel.build_view(
            _payload(actual=_bloque(closed=40, ttf=(30, 5), ttr=(28, 7)),
                     personas=["no soy un objeto", _persona(19, "Rodriguez, Saul")]),
            now=AHORA, min_n=8)
        self.assertEqual(len(v["rows"]), 1)


class ContratoTest(unittest.TestCase):
    def test_un_agregado_sin_current_es_error(self):
        with self.assertRaises(ValueError):
            weekly_panel.build_view({"previous": _bloque()}, now=AHORA)

    def test_el_stamp_de_siete_decimales_no_rompe_la_etiqueta(self):
        # Es un campo CALCULADO del flow, no de la lista: viene con siete
        # decimales de segundo. fromisoformat los acepta desde 3.11 y este test
        # existe para que un cambio de interprete no lo rompa en silencio.
        v = weekly_panel.build_view(_payload(), now=AHORA)
        self.assertEqual(v["previous_to_date"]["to"], "2026-08-26T15:18:02.0000000Z")
        self.assertEqual(weekly_panel._etiqueta_semana("2026-08-26T15:18:02.0000000Z"),
                         "semana desde mié 26 10:18")

    def test_la_etiqueta_de_semana_sale_en_hora_de_lima(self):
        v = weekly_panel.build_view(_payload(), now=AHORA)
        self.assertEqual(v["week_label"], "semana desde mié 2 07:30")

    def test_contadores_basura_valen_cero(self):
        v = weekly_panel.build_view(
            _payload(actual={"closed": "x", "ttf": "nada", "ttr": None}),
            now=AHORA)
        self.assertEqual(v["current"]["closed"], 0)
        self.assertEqual(v["current"]["ttf"]["total"], 0)


class CacheTest(unittest.IsolatedAsyncioTestCase):
    def _settings(self, url="https://flow.example/con-sas"):
        with mock.patch.dict("os.environ", {"ITSM_WEEKLY_FLOW_URL": url}):
            return weekly_panel.panel_settings({})

    async def test_una_lectura_por_ventana(self):
        llamadas = []

        def handler(request):
            llamadas.append(request.url)
            return httpx.Response(200, json=_payload())

        cache = weekly_panel.PanelCache(ttl_seconds=600)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await cache.get(self._settings(), c)
            await cache.get(self._settings(), c)
        self.assertEqual(len(llamadas), 1)

    async def test_un_fallo_sirve_la_ultima_vista_buena_marcada(self):
        respuestas = [httpx.Response(200, json=_payload()), httpx.Response(500)]

        def handler(request):
            return respuestas.pop(0)

        cache = weekly_panel.PanelCache(ttl_seconds=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await cache.get(self._settings(), c)
            v = await cache.get(self._settings(), c)
        # Una pantalla en blanco no se distingue de un cuelgue.
        self.assertEqual(v["previous"]["closed"], 107)

    async def test_el_detalle_de_un_fallo_http_no_se_loguea(self):
        # La URL lleva SAS y httpx la mete en el mensaje de sus excepciones.
        def handler(request):
            return httpx.Response(500)

        cache = weekly_panel.PanelCache(ttl_seconds=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with self.assertRaises(Exception):
                await cache.get(self._settings(), c)
        self.assertEqual(cache.last_error, "HTTPStatusError")
        self.assertNotIn("con-sas", cache.last_error)

    async def test_sin_url_configurada_lo_dice(self):
        cache = weekly_panel.PanelCache()
        async with httpx.AsyncClient() as c:
            with self.assertRaises(RuntimeError):
                await cache.get(self._settings(url=""), c)
        self.assertIn("ITSM_WEEKLY_FLOW_URL", cache.last_error)

    async def test_un_agregado_en_disco_sirve_para_iterar(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "weekly.json"
            ruta.write_text(json.dumps(_payload()))
            cache = weekly_panel.PanelCache()
            async with httpx.AsyncClient() as c:
                v = await cache.get(self._settings(url=f"file://{ruta}"), c)
        self.assertEqual(v["previous"]["closed"], 107)


class RenderTest(unittest.TestCase):
    def test_html_lleva_datos_endpoint_y_el_aviso_del_piso(self):
        v = weekly_panel.build_view(_payload(), now=AHORA)
        html = weekly_panel.render_html(v, poll_seconds=60)
        self.assertIn('id="datos"', html)
        self.assertIn("/api/weekly-panel", html)
        # El aviso del sesgo va EN PANTALLA, no solo en el bead.
        self.assertIn("el TTR es un piso", html)

    def test_el_json_embebido_no_puede_cerrar_el_script(self):
        v = weekly_panel.build_view(_payload(), now=AHORA)
        v["headline"] = "</script><script>alert(1)</script>"
        self.assertNotIn("<script>alert", weekly_panel.render_html(v, poll_seconds=60))


class SettingsTest(unittest.TestCase):
    def test_la_url_viene_del_entorno_y_es_otra(self):
        with mock.patch.dict("os.environ", {
                "ITSM_WEEKLY_FLOW_URL": "https://semanal.example/x",
                "ITSM_PANEL_FLOW_URL": "https://enriesgo.example/y"}):
            s = weekly_panel.panel_settings({})
        self.assertEqual(s["flow_url"], "https://semanal.example/x")

    def test_defaults_y_pisos(self):
        s = weekly_panel.panel_settings({"weekly_panel": {"refresh_seconds": 1}})
        self.assertEqual(s["refresh_seconds"], 60)
        self.assertEqual(s["min_n"], weekly_panel.DEFAULT_MIN_N)

    def test_bloque_basura_no_revienta(self):
        s = weekly_panel.panel_settings({"weekly_panel": "si"})
        self.assertEqual(s["max_rows"], weekly_panel.DEFAULT_MAX_ROWS)


if __name__ == "__main__":
    unittest.main()
