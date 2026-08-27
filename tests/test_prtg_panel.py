"""Tests del panel de estado de la red sobre PRTG (quiosco-fdc)."""

import os
import unittest
from unittest import mock

from quiosco import main, prtg_panel


def _sensor(sensor, *, status_raw=3, group="CORE", device="SW CORE 1",
            message="OK", since=""):
    """Sensor con la forma real que devuelve /api/table.json.

    PRTG entrega cada columna dos veces: la renderizada y la _raw. 'message'
    viene envuelto en HTML y solo message_raw trae texto plano; el fixture
    reproduce eso a proposito porque es facil leer la columna equivocada.
    """
    return {
        "objid": 1, "objid_raw": 1,
        "probe": "CIP", "probe_raw": "CIP",
        "group": group, "group_raw": group,
        "device": device, "device_raw": device,
        "sensor": sensor, "sensor_raw": sensor,
        "status": "Up", "status_raw": status_raw,
        "message": f'<div class="status">{message}<div class="moreicon"></div></div>',
        "message_raw": message,
        "downtimesince": since, "downtimesince_raw": since,
    }


class StatusMappingTest(unittest.TestCase):
    def test_estados_accionables_se_listan(self):
        for raw in (1, 4, 5, 6, 13, 14):
            with self.subTest(status_raw=raw):
                self.assertIn(prtg_panel.STATUS[raw][2], prtg_panel.LISTABLES,
                              f"status_raw {raw} deberia listarse como incidencia")

    def test_estados_no_accionables_no_se_listan(self):
        # Up, escaneando, pausados y unusual no exigen accion.
        for raw in (0, 2, 3, 7, 8, 9, 10, 11, 12):
            with self.subTest(status_raw=raw):
                self.assertNotIn(prtg_panel.STATUS[raw][2], prtg_panel.LISTABLES)

    def test_sin_datos_no_se_cuenta_como_caido(self):
        # El bug que motiva separar banda de categoria: "Sin datos" comparte el
        # color con los caidos reconocidos, pero NO es una caida. Sumarlos hacia
        # que el titular anunciara "3 sensores caidos" habiendo uno solo.
        raw = {"sensors": [_sensor("roto", status_raw=5),
                           _sensor("mudo1", status_raw=1),
                           _sensor("mudo2", status_raw=1)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["counts"]["caido"], 1)
        self.assertEqual(view["counts"]["sindatos"], 2)
        self.assertEqual(view["headline"], "1 sensor caído")
        self.assertIn("2 sin datos", view["subline"])

    def test_sin_datos_solo_manda_el_titular_si_no_hay_caidos(self):
        raw = {"sensors": [_sensor("mudo", status_raw=1)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["headline"], "1 sensor sin datos")
        self.assertEqual(view["severity"], "caida")

    def test_caido_reconocido_si_cuenta_como_caido(self):
        raw = {"sensors": [_sensor("ack", status_raw=13)]}
        self.assertEqual(prtg_panel.build_view(raw)["counts"]["caido"], 1)

    def test_pausado_y_unusual_no_usan_color_de_estado(self):
        # Los cuatro colores de estado quedan para lo que exige accion.
        self.assertEqual(prtg_panel.STATUS[7][0], "neutro")
        self.assertEqual(prtg_panel.STATUS[10][0], "inusual")

    def test_status_desconocido_no_revienta(self):
        raw = {"sensors": [dict(_sensor("X"), status_raw=999)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["total"], 1)
        self.assertEqual(view["issues"], [])

    def test_status_no_numerico_no_revienta(self):
        raw = {"sensors": [dict(_sensor("X"), status_raw=None)]}
        self.assertEqual(prtg_panel.build_view(raw)["total"], 1)


class BuildViewTest(unittest.TestCase):
    def test_todo_operativo(self):
        raw = {"sensors": [_sensor("A"), _sensor("B")]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["headline"], "Sin incidencias")
        self.assertEqual(view["severity"], "ok")
        self.assertFalse(view["problem"])
        self.assertEqual(view["counts"]["ok"], 2)

    def test_un_caido_manda_el_titular(self):
        raw = {"sensors": [_sensor("A"), _sensor("B", status_raw=5)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["severity"], "caida")
        self.assertEqual(view["headline"], "1 sensor caído")

    def test_solo_alertas_dan_severidad_leve(self):
        raw = {"sensors": [_sensor("A", status_raw=4), _sensor("B", status_raw=4)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["severity"], "leve")
        self.assertEqual(view["headline"], "2 sensores en alerta")

    def test_un_caido_gana_sobre_las_alertas(self):
        raw = {"sensors": [_sensor("A", status_raw=4), _sensor("B", status_raw=5)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["severity"], "caida")
        self.assertIn("1 en alerta", view["subline"])

    def test_lo_mas_grave_va_arriba(self):
        raw = {"sensors": [
            _sensor("alerta", status_raw=4),
            _sensor("sin datos", status_raw=1),
            _sensor("caido", status_raw=5),
        ]}
        bandas = [i["band"] for i in prtg_panel.build_view(raw)["issues"]]
        self.assertEqual(bandas, ["caida", "grave", "leve"])

    def test_usa_message_raw_y_no_el_html(self):
        raw = {"sensors": [_sensor("A", status_raw=5, message="100 % de CPU")]}
        issue = prtg_panel.build_view(raw)["issues"][0]
        self.assertEqual(issue["message"], "100 % de CPU")
        self.assertNotIn("<div", issue["message"])

    def test_unusual_se_cuenta_pero_no_se_lista(self):
        # 23 de 31 no-verdes son unusual con el mismo mensaje de trafico bajo:
        # listarlos ahogaria al unico caido.
        raw = {"sensors": [_sensor(f"u{i}", status_raw=10) for i in range(23)]
                          + [_sensor("roto", status_raw=5)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["counts"]["inusual"], 23)
        self.assertEqual([i["sensor"] for i in view["issues"]], ["roto"])
        self.assertIn("23 inusuales", view["subline"])

    def test_unusual_se_puede_pedir(self):
        raw = {"sensors": [_sensor("u", status_raw=10)]}
        view = prtg_panel.build_view(raw, show_unusual=True)
        self.assertEqual(len(view["issues"]), 1)

    def test_pausados_se_cuentan_y_se_avisan(self):
        # Un sensor pausado no se esta vigilando; callarlo seria mentir por
        # omision con un tercio del arbol pausado.
        raw = {"sensors": [_sensor(f"p{i}", status_raw=7) for i in range(138)]}
        view = prtg_panel.build_view(raw)
        self.assertEqual(view["paused"], 138)
        self.assertEqual(view["paused_note"], "138 sensores pausados sin vigilar")

    def test_sin_pausados_no_hay_aviso(self):
        view = prtg_panel.build_view({"sensors": [_sensor("A")]})
        self.assertEqual(view["paused_note"], "")

    def test_singular_del_aviso_de_pausados(self):
        raw = {"sensors": [_sensor("p", status_raw=7)]}
        self.assertEqual(prtg_panel.build_view(raw)["paused_note"],
                         "1 sensor pausado sin vigilar")

    def test_tope_de_filas_y_cuenta_de_omitidos(self):
        raw = {"sensors": [_sensor(f"s{i:02d}", status_raw=5) for i in range(20)]}
        view = prtg_panel.build_view(raw, max_issues=8)
        self.assertEqual(len(view["issues"]), 8)
        self.assertEqual(view["omitted"], 12)

    def test_sin_omitidos_cuando_caben(self):
        raw = {"sensors": [_sensor("s", status_raw=5)]}
        self.assertEqual(prtg_panel.build_view(raw, max_issues=8)["omitted"], 0)

    def test_grupos_ocultos_no_cuentan_ni_aparecen(self):
        raw = {"sensors": [_sensor("A", group="CORE"),
                           _sensor("B", group="PRINTERS", status_raw=5)]}
        view = prtg_panel.build_view(raw, hidden_groups=["printers"])
        self.assertEqual(view["total"], 1)
        self.assertEqual(view["issues"], [])
        self.assertEqual(view["severity"], "ok")

    def test_respuesta_sin_sensores_es_error(self):
        with self.assertRaises(ValueError):
            prtg_panel.build_view({"prtg-version": "25.4"})

    def test_lista_vacia_es_valida(self):
        view = prtg_panel.build_view({"sensors": []})
        self.assertEqual(view["total"], 0)
        self.assertEqual(view["severity"], "ok")


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = prtg_panel.panel_settings({})
        self.assertEqual(s["base_url"], prtg_panel.DEFAULT_BASE_URL)
        self.assertEqual(s["max_issues"], prtg_panel.DEFAULT_MAX_ISSUES)
        self.assertFalse(s["show_unusual"])
        self.assertEqual(s["username"], "")

    def test_credenciales_vienen_del_entorno_no_del_config(self):
        # config.json se lee por SSH, se respalda y se edita desde la consola:
        # no es lugar para un passhash.
        with mock.patch.dict(os.environ, {"PRTG_USER": "u", "PRTG_PASSHASH": "123"}):
            s = prtg_panel.panel_settings({"prtg_panel": {"username": "no-usar"}})
        self.assertEqual(s["username"], "u")
        self.assertEqual(s["passhash"], "123")

    def test_lee_el_bloque(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = prtg_panel.panel_settings({"prtg_panel": {
                "base_url": "https://otro/", "refresh_seconds": 120,
                "max_issues": 4, "show_unusual": True, "hidden_groups": ["X"],
            }})
        self.assertEqual(s["base_url"], "https://otro")   # sin barra final
        self.assertEqual(s["refresh_seconds"], 120)
        self.assertEqual(s["max_issues"], 4)
        self.assertTrue(s["show_unusual"])

    def test_pisos_y_basura(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = prtg_panel.panel_settings({"prtg_panel": {
                "refresh_seconds": 1, "max_issues": 0, "hidden_groups": "no-es-lista",
            }})
        self.assertEqual(s["refresh_seconds"], 15.0)
        self.assertEqual(s["max_issues"], 1)
        self.assertEqual(s["hidden_groups"], [])


class RenderTest(unittest.TestCase):
    def test_html_lleva_datos_y_endpoint(self):
        view = prtg_panel.build_view({"sensors": [_sensor("Salud de sistema", status_raw=5)]})
        html = prtg_panel.render_html(view, refresh_seconds=60)
        self.assertIn("Salud de sistema", html)
        self.assertIn("/api/prtg-panel", html)
        self.assertIn("60000)", html)

    def test_el_json_embebido_no_puede_cerrar_el_script(self):
        view = prtg_panel.build_view(
            {"sensors": [_sensor("</script><b>x", status_raw=5)]})
        html = prtg_panel.render_html(view, refresh_seconds=60)
        self.assertNotIn("</script><b>", html)

    def test_pagina_de_error(self):
        html = prtg_panel.render_error_html("timeout")
        self.assertIn("Estado de la red no disponible", html)


class PanelLinkIntegrationTest(unittest.TestCase):
    PANEL_URL = "http://172.25.21.37:8000/panel/prtg"

    def test_se_reconoce_como_panel_propio(self):
        self.assertTrue(main._is_panel_url(self.PANEL_URL))

    def test_el_iframe_sale_relativo(self):
        self.assertEqual(main._iframe_src(self.PANEL_URL), "/panel/prtg")

    def test_no_se_renderiza_como_screenshot(self):
        self.assertFalse(main._link_uses_screenshot({"id": "x", "url": self.PANEL_URL}))

    def test_entra_en_la_captura_de_gif_para_el_fallback(self):
        links = [{"id": "x", "url": self.PANEL_URL, "enabled": True, "zoom": 1.0}]
        urls, _ = main.gif_capture_targets(links, 1280, 720)
        self.assertEqual(urls, [self.PANEL_URL])


if __name__ == "__main__":
    unittest.main()
