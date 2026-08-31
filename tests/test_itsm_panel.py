"""Tests del panel de tickets por brechearse (quiosco-fdc / ITSM)."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

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
        # "Perez, Ana" se muestra como "Ana Perez", asi que las iniciales son
        # AP: siguen al nombre que se ve, no al que guarda SharePoint.
        v = itsm_panel.build_view(_payload([_ticket(1, nombre="Perez, Ana")]), now=AHORA)
        self.assertEqual(v["rows"][0]["name"], "Ana Perez")
        self.assertEqual(v["rows"][0]["initials"], "AP")
        self.assertEqual(v["rows"][0]["photo"], "")

    def test_show_people_apagado_borra_nombre_foto_e_iniciales(self):
        v = itsm_panel.build_view(_payload([_ticket(1, nombre="Perez, Ana", aid=19)]),
                                  now=AHORA, show_people=False,
                                  photo_ids=frozenset({"19"}))
        r = v["rows"][0]
        self.assertEqual((r["name"], r["initials"], r["photo"]), ("", "", ""))

    def test_la_foto_sale_de_disco_cuando_el_archivo_existe(self):
        v = itsm_panel.build_view(_payload([_ticket(1, aid=6554)]), now=AHORA,
                                  photo_ids=frozenset({"6554"}))
        self.assertEqual(v["rows"][0]["photo"], "/static/photos/6554.jpg")

    def test_sin_archivo_no_hay_foto_y_quedan_las_iniciales(self):
        # 1 de cada 12 personas activas no tiene foto: esto es camino principal.
        v = itsm_panel.build_view(_payload([_ticket(1, nombre="Matos, Diana", aid=6690)]),
                                  now=AHORA, photo_ids=frozenset({"6554"}))
        self.assertEqual(v["rows"][0]["photo"], "")
        self.assertEqual(v["rows"][0]["initials"], "DM")

    def test_el_payload_no_puede_inyectar_una_foto(self):
        # Las fotos vienen de disco, no del flow: si el agregado trajera un
        # data: URI se ignora, para que no haya dos mecanismos.
        t = _ticket(1, aid=6554)
        t["assignee"]["photo"] = "data:image/jpeg;base64,zz"
        v = itsm_panel.build_view(_payload([t]), now=AHORA)
        self.assertEqual(v["rows"][0]["photo"], "")

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


class WeekStartTest(unittest.TestCase):
    """El corte vive en la config de quiosco, no en el flow: no es un hecho de
    los datos como el calendario habil, es cuando se reune el equipo."""

    def _cut(self, cuando):
        return itsm_panel.week_start(cuando).strftime("%Y-%m-%d %H:%M")

    def test_lunes_mira_al_miercoles_anterior(self):
        # Lunes 2026-08-31 16:00Z = 11:00 Lima -> el corte fue el mie 26.
        self.assertEqual(self._cut(datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)),
                         "2026-08-26 07:30")

    def test_el_miercoles_antes_de_la_hora_sigue_en_la_semana_vieja(self):
        # Mie 2026-09-02 11:00Z = 06:00 Lima, antes de las 07:30: la reunion aun
        # no paso, asi que la semana en curso empezo hace siete dias.
        self.assertEqual(self._cut(datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)),
                         "2026-08-26 07:30")

    def test_el_miercoles_pasada_la_hora_abre_semana(self):
        self.assertEqual(self._cut(datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)),
                         "2026-09-02 07:30")

    def test_nunca_devuelve_un_corte_futuro(self):
        for dia in range(1, 15):
            cuando = datetime(2026, 9, dia, 18, 0, tzinfo=timezone.utc)
            with self.subTest(dia=dia):
                self.assertLessEqual(itsm_panel.week_start(cuando),
                                     cuando.astimezone(itsm_panel.LIMA))

    def test_se_puede_mover_el_corte_sin_tocar_codigo(self):
        # Jueves 09:00 en vez de miercoles 07:30.
        cuando = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)   # lunes
        self.assertEqual(itsm_panel.week_start(cuando, 4, "09:00").strftime("%Y-%m-%d %H:%M"),
                         "2026-08-27 09:00")

    def test_hora_malformada_cae_al_default(self):
        cuando = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(itsm_panel.week_start(cuando, 3, "basura").strftime("%H:%M"), "07:30")

    def test_el_payload_ya_no_manda_la_semana(self):
        # El flow sigue emitiendo week_start pero viaja de pasajero.
        raro = dict(_payload([]), week_start="2099-01-01T00:00:00Z")
        v = itsm_panel.build_view(raro, now=AHORA)
        self.assertNotIn("2099", v["week_label"])


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



def _settings(**over):
    """Settings reales, no un dict a mano: si el modulo gana una clave, estos
    tests la heredan en vez de romperse con un KeyError."""
    with mock.patch.dict(os.environ, {"ITSM_PANEL_FLOW_URL": "https://flow.example/run"}):
        s = itsm_panel.panel_settings({})
    s.update(over)
    return s


class TransportTest(unittest.IsolatedAsyncioTestCase):
    """El trigger del flow es GET. Postear devuelve 4xx y el panel lo mostraria
    como 'fuente caida', que es un sintoma que despista."""

    async def _pedir(self, payload=None):
        visto = {}

        def handler(request: httpx.Request) -> httpx.Response:
            visto["method"] = request.method
            visto["url"] = str(request.url)
            return httpx.Response(200, json=payload if payload is not None else _payload([]))

        cache = itsm_panel.PanelCache(ttl_seconds=300)
        settings = _settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            view = await cache.get(settings, c)
        return visto, view, cache

    async def test_usa_get(self):
        visto, _, _ = await self._pedir()
        self.assertEqual(visto["method"], "GET")

    async def test_segunda_lectura_sale_de_cache_sin_repegarle_al_flow(self):
        llamadas = {"n": 0}

        def handler(request):
            llamadas["n"] += 1
            return httpx.Response(200, json=_payload([]))

        cache = itsm_panel.PanelCache(ttl_seconds=300)
        settings = _settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await cache.get(settings, c)
            await cache.get(settings, c)
        self.assertEqual(llamadas["n"], 1)

    async def test_un_agregado_malformado_no_desplaza_al_ultimo_bueno(self):
        # El due se calcula contra el reloj REAL, no contra la referencia fija de
        # los otros tests: la cache mide la cuenta regresiva contra ahora, que es
        # justamente lo que la hace exacta entre refrescos del flow.
        futuro = (datetime.now(timezone.utc) + timedelta(hours=2)
                  ).isoformat().replace("+00:00", "Z")
        bueno = _payload([dict(_ticket(1), due=futuro)])
        respuestas = [bueno, {"basura": True}]

        def handler(request):
            return httpx.Response(200, json=respuestas.pop(0) if respuestas else {})

        cache = itsm_panel.PanelCache(ttl_seconds=0)   # siempre refresca
        settings = _settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await cache.get(settings, c)
            view = await cache.get(settings, c)      # llega basura
        self.assertEqual([r["id"] for r in view["rows"]], [1])

    async def test_sin_url_configurada_no_inventa_una_vista(self):
        cache = itsm_panel.PanelCache()
        settings = _settings(flow_url="")
        async with httpx.AsyncClient() as c:
            with self.assertRaises(RuntimeError):
                await cache.get(settings, c)


class DisplayNameTest(unittest.TestCase):
    """SharePoint entrega "Apellido, Nombre  (ORG)"; la pared quiere otra cosa."""

    def test_voltea_apellido_nombre(self):
        self.assertEqual(itsm_panel.display_name("Rodriguez, Saul"), "Saul Rodriguez")

    def test_quita_el_sufijo_de_organizacion_y_el_doble_espacio(self):
        self.assertEqual(itsm_panel.display_name("Rodriguez, Saul  (CIP)"),
                         "Saul Rodriguez")

    def test_dos_apellidos_sobreviven(self):
        # El caso peruano: la coma marca donde termina el apellido, asi que
        # voltear por la coma es seguro y partir por espacios no lo seria.
        self.assertEqual(itsm_panel.display_name("Garcia Perez, Juan Carlos  (CIP)"),
                         "Juan Carlos Garcia Perez")

    def test_sin_coma_se_deja_como_esta(self):
        self.assertEqual(itsm_panel.display_name("Peris Waithira"), "Peris Waithira")

    def test_dos_comas_no_se_tocan(self):
        # No se sabe cual separa que; mejor mostrarlo crudo que inventar.
        self.assertEqual(itsm_panel.display_name("Uno, Dos, Tres"), "Uno, Dos, Tres")

    def test_vacio(self):
        self.assertEqual(itsm_panel.display_name(""), "")
        self.assertEqual(itsm_panel.display_name(None), "")

    def test_las_iniciales_siguen_al_nombre_mostrado(self):
        v = itsm_panel.build_view(
            _payload([_ticket(1, nombre="Rodriguez, Saul  (CIP)")]), now=AHORA)
        self.assertEqual(v["rows"][0]["name"], "Saul Rodriguez")
        self.assertEqual(v["rows"][0]["initials"], "SR")


class WeekLabelTest(unittest.TestCase):
    def test_el_dia_sale_en_espanol(self):
        # strftime("%a") daba "Wed" con el locale C del contenedor.
        v = itsm_panel.build_view(_payload([]), now=AHORA)
        self.assertIn("mié", v["week_label"])
        self.assertNotIn("Wed", v["week_label"])


class DisplayNameBordesTest(unittest.TestCase):
    def test_sin_espacio_despues_de_la_coma(self):
        # Caso real de la lista: "Zamudio,Tatiana (CIP)". Separar por ", " se lo
        # comeria; hay que separar por "," y hacer trim.
        self.assertEqual(itsm_panel.display_name("Zamudio,Tatiana (CIP)"),
                         "Tatiana Zamudio")

    def test_espacios_raros_alrededor_de_la_coma(self):
        self.assertEqual(itsm_panel.display_name("  Matos ,  Diana  (CIP) "),
                         "Diana Matos")

if __name__ == "__main__":
    unittest.main()
