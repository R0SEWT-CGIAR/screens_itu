"""Tests del panel propio de estado de servicios (quiosco-pki)."""

import unittest
from datetime import datetime, timezone

from quiosco import main, uptime_panel


def _monitor(name, *, status="success", ratios=None, ratio90=None, last=None, mid=1):
    """Monitor con la forma que devuelve el JSON de la status page."""
    ratios = [100.0] * 90 if ratios is None else ratios
    raw = {
        "monitorId": mid,
        "name": name,
        "statusClass": status,
        "type": "HTTP(s)",
        "dailyRatios": [{"date": f"2026-06-{(i % 28) + 1:02d}", "ratio": f"{r:.3f}"}
                        for i, r in enumerate(ratios)],
        "90dRatio": {"ratio": f"{ratio90 if ratio90 is not None else min(ratios):.3f}"},
    }
    if last:
        raw["lastDowntime"] = last
    return raw


class BandsTest(unittest.TestCase):
    def test_bandas_por_umbral(self):
        self.assertEqual(uptime_panel.band_for(100.0), "ok")
        self.assertEqual(uptime_panel.band_for(99.5), "leve")
        self.assertEqual(uptime_panel.band_for(99.0), "leve")
        self.assertEqual(uptime_panel.band_for(97.0), "grave")
        self.assertEqual(uptime_panel.band_for(50.0), "caida")
        self.assertEqual(uptime_panel.band_for(0.0), "caida")


class RatioTextTest(unittest.TestCase):
    def test_cien_exacto_se_escribe_corto(self):
        self.assertEqual(uptime_panel.ratio_text(100.0), "100")

    def test_se_trunca_y_nunca_se_redondea_a_cien(self):
        # El bug que motiva el truncado: 99.996 redondeado a dos decimales da
        # "100.00", y el panel afirmaria un 100% que no ocurrio, contradiciendo
        # ademas su propia nota de "dias con incidencias".
        self.assertEqual(uptime_panel.ratio_text(99.996), "99.99")
        self.assertEqual(uptime_panel.ratio_text(99.999), "99.99")

    def test_dos_decimales_normales(self):
        self.assertEqual(uptime_panel.ratio_text(98.413), "98.41")


class CellsTest(unittest.TestCase):
    def test_noventa_dias_dan_treinta_celdas(self):
        view = uptime_panel.build_view({"data": [_monitor("A")]})
        self.assertEqual(len(view["monitors"][0]["cells"]), 30)

    def test_la_celda_toma_el_peor_del_trio(self):
        # Un dia malo entre dos limpios no se puede promediar hasta desaparecer.
        ratios = [100.0] * 90
        ratios[1] = 42.0
        view = uptime_panel.build_view({"data": [_monitor("A", ratios=ratios)]})
        cells = view["monitors"][0]["cells"]
        self.assertEqual(cells[0]["band"], "caida")
        self.assertTrue(all(c["band"] == "ok" for c in cells[1:]))

    def test_cuenta_de_celdas_con_incidencias(self):
        ratios = [100.0] * 90
        ratios[0] = 99.5
        ratios[80] = 99.5
        view = uptime_panel.build_view({"data": [_monitor("A", ratios=ratios)]})
        self.assertEqual(view["monitors"][0]["bad_cells"], 2)

    def test_historial_corto_no_revienta(self):
        view = uptime_panel.build_view({"data": [_monitor("A", ratios=[100.0] * 4)]})
        self.assertEqual(len(view["monitors"][0]["cells"]), 2)


class BuildViewTest(unittest.TestCase):
    def test_todo_operativo(self):
        view = uptime_panel.build_view({"data": [_monitor("A", mid=1), _monitor("B", mid=2)]})
        self.assertEqual(view["headline"], "2 de 2 operativos")
        self.assertFalse(view["problem"])
        self.assertEqual(view["down"], 0)

    def test_un_caido_encabeza_y_marca_problema(self):
        raw = {"data": [_monitor("Aaa", mid=1), _monitor("Zzz", status="danger", mid=2)]}
        view = uptime_panel.build_view(raw)
        self.assertTrue(view["problem"])
        self.assertEqual(view["headline"], "1 de 2 caído")
        # Lo roto va arriba aunque alfabeticamente fuera ultimo.
        self.assertEqual(view["monitors"][0]["name"], "Zzz")
        self.assertTrue(view["monitors"][0]["down"])

    def test_plural_de_caidos(self):
        raw = {"data": [_monitor("A", status="danger", mid=1),
                        _monitor("B", status="danger", mid=2)]}
        self.assertEqual(uptime_panel.build_view(raw)["headline"], "2 de 2 caídos")

    def test_estado_no_success_cuenta_como_caido(self):
        # paused/warning tambien: cualquier cosa que no sea success no esta bien.
        raw = {"data": [_monitor("A", status="paused", mid=1)]}
        self.assertTrue(uptime_panel.build_view(raw)["monitors"][0]["down"])

    def test_orden_estable_entre_refrescos(self):
        raw = {"data": [_monitor("Bbb", mid=1), _monitor("aaa", mid=2)]}
        nombres = [m["name"] for m in uptime_panel.build_view(raw)["monitors"]]
        self.assertEqual(nombres, ["aaa", "Bbb"])

    def test_alias_reemplaza_la_etiqueta_pero_no_el_nombre(self):
        raw = {"data": [_monitor("KM Hub/cipweb2")]}
        view = uptime_panel.build_view(raw, aliases={"KM Hub/cipweb2": "KM Hub"})
        self.assertEqual(view["monitors"][0]["label"], "KM Hub")
        self.assertEqual(view["monitors"][0]["name"], "KM Hub/cipweb2")

    def test_ocultos_por_nombre_no_aparecen_ni_cuentan(self):
        raw = {"data": [_monitor("visible", mid=1), _monitor("secreto", mid=2)]}
        view = uptime_panel.build_view(raw, hidden=["SECRETO"])
        self.assertEqual(view["total"], 1)
        self.assertEqual([m["name"] for m in view["monitors"]], ["visible"])

    def test_ocultos_por_id(self):
        raw = {"data": [_monitor("a", mid=11), _monitor("b", mid=22)]}
        view = uptime_panel.build_view(raw, hidden=["22"])
        self.assertEqual([m["name"] for m in view["monitors"]], ["a"])

    def test_el_orden_usa_el_alias_no_el_nombre(self):
        raw = {"data": [_monitor("Zorro", mid=1), _monitor("Alfa", mid=2)]}
        view = uptime_panel.build_view(raw, aliases={"Zorro": "Aaa"})
        self.assertEqual([m["label"] for m in view["monitors"]], ["Aaa", "Alfa"])

    def test_incidente_mas_reciente_formateado(self):
        raw = {"data": [
            _monitor("A", mid=1, last={"date": "2026-06-01 03:00:00", "duration": 120}),
            _monitor("B", mid=2, last={"date": "2026-06-09 03:10:52", "duration": 17400}),
        ]}
        view = uptime_panel.build_view(raw, now=datetime(2026, 6, 13, 10, 0, 0))
        self.assertEqual(view["incident"]["when"], "hace 4 días")
        self.assertEqual(view["incident"]["duration"], "4 h 50 min")

    def test_incidente_de_hoy_y_de_ayer(self):
        base = {"data": [_monitor("A", last={"date": "2026-06-13 08:00:00", "duration": 60})]}
        hoy = uptime_panel.build_view(base, now=datetime(2026, 6, 13, 10, 0, 0))
        self.assertEqual(hoy["incident"]["when"], "hoy")
        ayer = uptime_panel.build_view(base, now=datetime(2026, 6, 14, 10, 0, 0))
        self.assertEqual(ayer["incident"]["when"], "ayer")

    def test_sin_incidentes(self):
        self.assertIsNone(uptime_panel.build_view({"data": [_monitor("A")]})["incident"])

    def test_incidente_de_un_monitor_oculto_igual_se_considera(self):
        # El ocultamiento es de la fila en pantalla, no de la realidad operativa.
        raw = {"data": [_monitor("visible", mid=1),
                        _monitor("oculto", mid=2,
                                 last={"date": "2026-06-09 03:00:00", "duration": 600})]}
        view = uptime_panel.build_view(raw, hidden=["oculto"],
                                       now=datetime(2026, 6, 10, 10, 0, 0))
        self.assertIsNotNone(view["incident"])

    def test_json_sin_data_es_error(self):
        with self.assertRaises(ValueError):
            uptime_panel.build_view({"status": "ok"})

    def test_monitor_sin_ratio90_cae_al_peor_dia(self):
        raw = {"data": [{"monitorId": 1, "name": "A", "statusClass": "success",
                         "dailyRatios": [{"date": "2026-06-01", "ratio": "98.000"}]}]}
        view = uptime_panel.build_view(raw)
        self.assertEqual(view["monitors"][0]["ratio_text"], "98.00")


class TimezoneTest(unittest.TestCase):
    """El contenedor corre en UTC; la status page declara su zona en el JSON."""

    def test_usa_el_offset_de_la_status_page(self):
        raw = {"psp": {"timezone": "-05:00"}, "data": []}
        ahora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = ahora_utc - uptime_panel.local_now(raw)
        self.assertAlmostEqual(delta.total_seconds(), 5 * 3600, delta=5)

    def test_offset_positivo(self):
        raw = {"psp": {"timezone": "+02:00"}, "data": []}
        ahora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = uptime_panel.local_now(raw) - ahora_utc
        self.assertAlmostEqual(delta.total_seconds(), 2 * 3600, delta=5)

    def test_sin_zona_cae_al_reloj_local(self):
        delta = abs((uptime_panel.local_now({"data": []}) - datetime.now()).total_seconds())
        self.assertLess(delta, 5)

    def test_zona_ilegible_no_revienta(self):
        for basura in ("", "America/Lima", "-5", None, 12345):
            with self.subTest(basura=basura):
                raw = {"psp": {"timezone": basura}, "data": []}
                self.assertIsInstance(uptime_panel.local_now(raw), datetime)

    def test_el_reloj_del_panel_sale_en_hora_de_la_status_page(self):
        raw = {"psp": {"timezone": "-05:00"}, "data": []}
        esperado = uptime_panel.local_now(raw).strftime("%H:%M")
        self.assertEqual(uptime_panel.build_view(raw)["clock"], esperado)


class SettingsTest(unittest.TestCase):
    def test_defaults_sin_bloque(self):
        s = uptime_panel.panel_settings({})
        self.assertEqual(s["source_url"], uptime_panel.DEFAULT_SOURCE_URL)
        self.assertEqual(s["aliases"], {})
        self.assertEqual(s["hidden"], [])

    def test_lee_el_bloque(self):
        s = uptime_panel.panel_settings({"uptime_panel": {
            "source_url": "https://ejemplo/x", "refresh_seconds": 120,
            "aliases": {"a": "b"}, "hidden": ["c"],
        }})
        self.assertEqual(s["source_url"], "https://ejemplo/x")
        self.assertEqual(s["refresh_seconds"], 120)
        self.assertEqual(s["aliases"], {"a": "b"})

    def test_refresco_tiene_piso(self):
        # Sin piso, un 1 en el config martillearia UptimeRobot desde 3 pantallas.
        s = uptime_panel.panel_settings({"uptime_panel": {"refresh_seconds": 1}})
        self.assertEqual(s["refresh_seconds"], 15.0)

    def test_bloque_malformado_no_revienta(self):
        s = uptime_panel.panel_settings({"uptime_panel": {"aliases": "no-es-dict",
                                                          "hidden": "tampoco",
                                                          "refresh_seconds": "ni-esto"}})
        self.assertEqual(s["aliases"], {})
        self.assertEqual(s["hidden"], [])
        self.assertEqual(s["refresh_seconds"], uptime_panel.DEFAULT_REFRESH_SECONDS)


class RenderTest(unittest.TestCase):
    def test_html_lleva_los_datos_embebidos_y_el_intervalo(self):
        view = uptime_panel.build_view({"data": [_monitor("Servicio X")]})
        html = uptime_panel.render_html(view, refresh_seconds=60)
        self.assertIn("Servicio X", html)
        self.assertIn('id="datos"', html)
        self.assertIn("setInterval(refrescar, 60000)", html)

    def test_el_json_embebido_no_puede_cerrar_el_script(self):
        view = uptime_panel.build_view({"data": [_monitor("</script><b>x")]})
        html = uptime_panel.render_html(view, refresh_seconds=60)
        self.assertNotIn("</script><b>", html)

    def test_pagina_de_error(self):
        html = uptime_panel.render_error_html("timeout")
        self.assertIn("no disponible", html)
        self.assertIn("timeout", html)



class PanelLinkIntegrationTest(unittest.TestCase):
    """El panel es una pagina del propio quiosco, no un sitio externo.

    Se guarda en config.json con URL absoluta sobre PROXY_BASE, porque el loop
    de GIF necesita navegar a algo real y la clave del asset se deriva de esa
    misma cadena; pero el iframe tiene que salir relativo.
    """

    PANEL_URL = "http://172.25.21.37:8000/panel/uptime"

    def test_se_reconoce_como_panel_propio(self):
        self.assertTrue(main._is_panel_url(self.PANEL_URL))
        self.assertFalse(main._is_panel_url("https://stats.uptimerobot.com/26r4CjSckG"))
        self.assertFalse(main._is_panel_url("https://172.25.0.22/public/mapshow.htm"))

    def test_el_iframe_sale_relativo_y_sin_proxy(self):
        # Mismo origen que la display page: pasarlo por /p/ seria proxiarse a si
        # mismo, y ademas romperia el fetch de /api/uptime-panel de la pagina.
        self.assertEqual(main._iframe_src(self.PANEL_URL), "/panel/uptime")

    def test_no_se_renderiza_como_screenshot(self):
        link = {"id": "x", "url": self.PANEL_URL, "enabled": True}
        self.assertFalse(main._link_uses_screenshot(link))

    def test_entra_igual_en_la_captura_de_gif_para_el_fallback(self):
        # Se muestra por iframe, pero sin GIF el Default Media Receiver no
        # tendria que castear si DashCast se cae.
        links = [{"id": "x", "url": self.PANEL_URL, "enabled": True, "zoom": 1.0}]
        urls, _ = main.gif_capture_targets(links, 1280, 720)
        self.assertEqual(urls, [self.PANEL_URL])

    def test_la_url_del_config_es_navegable_para_playwright(self):
        # Si fuera relativa, page.goto() del loop de GIF no tendria host.
        from urllib.parse import urlparse
        parsed = urlparse(self.PANEL_URL)
        self.assertIn(parsed.scheme, ("http", "https"))
        self.assertTrue(parsed.netloc)

if __name__ == "__main__":
    unittest.main()
