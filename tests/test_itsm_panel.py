"""Tests del panel de tickets por brechearse (quiosco-fdc / ITSM)."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from quiosco import itsm_panel

AHORA = datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc)


def _ticket(tid, *, kind="TTR", horas=2.0, priority=5, nombre="Rodriguez, Saul", aid=19,
            photo=""):
    quien = {"id": aid, "name": nombre, "photo": photo} if aid else {}
    return {
        "id": tid, "kind": kind, "priority": priority,
        "due": (AHORA + timedelta(hours=horas)).isoformat().replace("+00:00", "Z"),
        "assignee": quien,
    }


def _payload(tickets, **counts):
    base = {"breached_ttf": 0, "breached_ttr": 0, "untriaged": 0, "waiting_user": 0}
    base.update(counts)
    return {
        "generated_at": AHORA.isoformat().replace("+00:00", "Z"),
        "week_start": "2026-08-26T12:30:00Z",
        "at_risk": tickets,
        "counts": base,
    }


class RemainingTest(unittest.TestCase):
    def test_unidades_legibles_de_lejos(self):
        self.assertEqual(itsm_panel.remaining_text(0.0), "ahora")
        self.assertEqual(itsm_panel.remaining_text(0.5), "30 min")
        self.assertEqual(itsm_panel.remaining_text(1.5), "1.5 h")
        self.assertEqual(itsm_panel.remaining_text(6.0), "6 h")
        self.assertEqual(itsm_panel.remaining_text(72.0), "3 d")

    def test_bandas(self):
        self.assertEqual(itsm_panel.band_for(0.2), "caida")
        self.assertEqual(itsm_panel.band_for(3.9), "leve")
        self.assertEqual(itsm_panel.band_for(4.0), "neutro")


class InitialsTest(unittest.TestCase):
    def test_apellido_coma_nombre(self):
        self.assertEqual(itsm_panel.initials("Rodriguez, Saul"), "RS")

    def test_un_solo_termino(self):
        self.assertEqual(itsm_panel.initials("Cher"), "CH")

    def test_vacio_o_basura(self):
        self.assertEqual(itsm_panel.initials(""), "?")
        self.assertEqual(itsm_panel.initials("   "), "?")
        self.assertEqual(itsm_panel.initials(None), "?")


class BuildViewTest(unittest.TestCase):
    def test_lista_vacia_es_estado_sano(self):
        v = itsm_panel.build_view(_payload([]), now=AHORA)
        self.assertEqual(v["severity"], "ok")
        self.assertEqual(v["headline"], "Nada por brechearse")
        self.assertFalse(v["problem"])

    def test_ordena_por_urgencia(self):
        v = itsm_panel.build_view(
            _payload([_ticket(3, horas=5), _ticket(1, horas=0.5), _ticket(2, horas=2)]),
            now=AHORA)
        self.assertEqual([r["id"] for r in v["rows"]], [1, 2, 3])
        self.assertEqual(v["subline"], "el más urgente en 30 min")

    def test_los_ya_vencidos_no_se_listan(self):
        # El panel es de los que todavia se pueden salvar; los vencidos van en
        # los contadores del equipo, sin desglose.
        v = itsm_panel.build_view(
            _payload([_ticket(1, horas=-3), _ticket(2, horas=1)]), now=AHORA)
        self.assertEqual([r["id"] for r in v["rows"]], [2])

    def test_el_horizonte_es_red_de_seguridad_no_criterio(self):
        # Quien selecciona es el flow, en dias habiles. Un viernes, algo que
        # vence el martes esta a ~90 h de pared y DEBE seguir mostrandose: si el
        # panel recortara a 48 h escondaria lo que el flow eligio, y se vaciaria
        # todo el fin de semana afirmando que no hay riesgo.
        v = itsm_panel.build_view(_payload([_ticket(1, horas=90)]), now=AHORA)
        self.assertEqual([r["id"] for r in v["rows"]], [1])

    def test_el_tope_igual_atrapa_un_flow_con_bug(self):
        v = itsm_panel.build_view(_payload([_ticket(1, horas=800)]), now=AHORA)
        self.assertEqual(v["rows"], [])

    def test_severidad_por_urgencia(self):
        urgente = itsm_panel.build_view(_payload([_ticket(1, horas=0.5)]), now=AHORA)
        self.assertEqual(urgente["severity"], "caida")
        holgado = itsm_panel.build_view(_payload([_ticket(1, horas=10)]), now=AHORA)
        self.assertEqual(holgado["severity"], "leve")

    def test_tope_de_filas_y_omitidos(self):
        v = itsm_panel.build_view(
            _payload([_ticket(i, horas=i) for i in range(1, 15)]),
            now=AHORA, max_rows=8)
        self.assertEqual(len(v["rows"]), 8)
        self.assertEqual(v["omitted"], 6)

    def test_contadores_del_equipo_pasan_tal_cual(self):
        v = itsm_panel.build_view(
            _payload([], breached_ttf=35, breached_ttr=30, untriaged=26, waiting_user=64),
            now=AHORA)
        self.assertEqual(v["counts"]["breached_ttf"], 35)
        self.assertEqual(v["counts"]["waiting_user"], 64)

    def test_contadores_ausentes_o_basura_son_cero(self):
        v = itsm_panel.build_view({"at_risk": [], "counts": {"breached_ttf": "x"}}, now=AHORA)
        self.assertEqual(v["counts"]["breached_ttf"], 0)
        self.assertEqual(v["counts"]["untriaged"], 0)

    def test_sin_asignar_se_marca(self):
        t = _ticket(1, aid=None)
        v = itsm_panel.build_view(_payload([t]), now=AHORA)
        self.assertTrue(v["rows"][0]["unassigned"])

    def test_iniciales_cuando_no_hay_foto(self):
        # El conector devuelve 404 para quien no tiene foto: las iniciales son
        # el camino normal para varios, no un caso raro.
        v = itsm_panel.build_view(_payload([_ticket(1, nombre="Perez, Ana")]), now=AHORA)
        self.assertEqual(v["rows"][0]["initials"], "PA")
        self.assertEqual(v["rows"][0]["photo"], "")

    def test_show_people_apagado_borra_nombre_foto_e_iniciales(self):
        v = itsm_panel.build_view(
            _payload([_ticket(1, nombre="Perez, Ana", photo="data:image/jpeg;base64,zz")]),
            now=AHORA, show_people=False)
        r = v["rows"][0]
        self.assertEqual((r["name"], r["initials"], r["photo"]), ("", "", ""))

    def test_stamp_sin_zona_se_lee_como_utc(self):
        # SharePoint emite UTC en raw; tratarlo como hora local corre el reloj 5 h.
        t = dict(_ticket(1, horas=2), due="2026-08-28T18:00:00")
        v = itsm_panel.build_view(_payload([t]), now=AHORA)
        self.assertEqual(v["rows"][0]["remaining"], "2 h")

    def test_due_invalido_se_descarta_sin_reventar(self):
        v = itsm_panel.build_view(
            _payload([dict(_ticket(1), due="no-es-fecha"), _ticket(2, horas=1)]), now=AHORA)
        self.assertEqual([r["id"] for r in v["rows"]], [2])

    def test_payload_sin_at_risk_es_error(self):
        with self.assertRaises(ValueError):
            itsm_panel.build_view({"counts": {}}, now=AHORA)

    def test_reloj_y_semana_en_hora_de_lima(self):
        v = itsm_panel.build_view(_payload([]), now=AHORA)
        self.assertEqual(v["clock"], "11:00")          # 16:00Z = 11:00 Lima
        self.assertIn("07:30", v["week_label"])        # mie 07:30 Lima = 12:30Z


class SettingsTest(unittest.TestCase):
    def test_la_url_del_flow_viene_del_entorno(self):
        # Lleva el SAS: no puede vivir en config.json.
        with mock.patch.dict(os.environ, {"ITSM_PANEL_FLOW_URL": "https://x/y"}):
            s = itsm_panel.panel_settings({"itsm_panel": {"flow_url": "no-usar"}})
        self.assertEqual(s["flow_url"], "https://x/y")

    def test_defaults_y_pisos(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = itsm_panel.panel_settings({"itsm_panel": {"refresh_seconds": 1,
                                                          "poll_seconds": 1}})
        self.assertEqual(s["flow_url"], "")
        self.assertEqual(s["refresh_seconds"], 60)
        self.assertEqual(s["poll_seconds"], 15)
        self.assertTrue(s["show_people"])


class RenderTest(unittest.TestCase):
    def test_html_lleva_datos_y_endpoint(self):
        v = itsm_panel.build_view(_payload([_ticket(65036, horas=0.03)]), now=AHORA)
        html = itsm_panel.render_html(v, poll_seconds=60)
        self.assertIn("65036", html)
        self.assertIn("/api/itsm-panel", html)

    def test_el_json_embebido_no_puede_cerrar_el_script(self):
        v = itsm_panel.build_view(_payload([_ticket(1, nombre="</script><b>x")]), now=AHORA)
        self.assertNotIn("</script><b>", itsm_panel.render_html(v, poll_seconds=60))


if __name__ == "__main__":
    unittest.main()
