"""Tests del panel de como va la cola (quiosco-2jp.1)."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quiosco import cola_panel, itsm_panel

AHORA = datetime(2026, 9, 2, 16, 0, 0, tzinfo=timezone.utc)


def _muestras(cuantas, *, cada_minutos=5, ttf=60, ttr=40, untriaged=50,
              waiting=80, active=90, paso_ttf=0):
    """Serie sintetica que termina justo en AHORA."""
    salida = []
    for i in range(cuantas):
        at = AHORA - timedelta(minutes=cada_minutos * (cuantas - 1 - i))
        salida.append({
            "at": at,
            "breached_ttf": ttf + paso_ttf * i,
            "breached_ttr": ttr,
            "untriaged": untriaged,
            "waiting_user": waiting,
            "active": active,
        })
    return salida


class HistoriaTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "data" / "cola-history.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_la_serie_sobrevive_al_reinicio(self):
        # Es la razon de existir del archivo: una serie que se borra en cada
        # despliegue no contesta "veniamos de 40 o de 90".
        h = cola_panel.ColaHistory(self.path)
        h.append({"breached_ttf": 61, "active": 80}, now=AHORA - timedelta(minutes=5))
        h.append({"breached_ttf": 63, "active": 82}, now=AHORA)

        otra = cola_panel.ColaHistory(self.path)
        self.assertEqual(otra.load(now=AHORA), 2)
        self.assertEqual([m["breached_ttf"] for m in otra.samples], [61, 63])
        self.assertEqual(otra.samples[-1]["at"], AHORA)

    def test_una_linea_partida_no_se_lleva_la_serie(self):
        # El archivo se escribe con append: un contenedor muerto a media
        # escritura deja una linea a medias. Perder esa muestra es aceptable.
        h = cola_panel.ColaHistory(self.path)
        h.append({"breached_ttf": 61}, now=AHORA - timedelta(minutes=5))
        with self.path.open("a") as f:
            f.write('{"at": "2026-09-02T15:5\n')
        h.append({"breached_ttf": 63}, now=AHORA)

        otra = cola_panel.ColaHistory(self.path)
        self.assertEqual(otra.load(now=AHORA), 2)

    def test_la_poda_deja_el_archivo_al_dia(self):
        h = cola_panel.ColaHistory(self.path, retention_days=1)
        h.append({"breached_ttf": 1}, now=AHORA - timedelta(days=3))
        h.append({"breached_ttf": 2}, now=AHORA - timedelta(hours=2))
        h.append({"breached_ttf": 3}, now=AHORA)

        self.assertEqual([m["breached_ttf"] for m in h.samples], [2, 3])
        # Y en disco tambien: si solo se podara en memoria, el proximo load
        # volveria a traer lo viejo.
        lineas = [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
        self.assertEqual([x["breached_ttf"] for x in lineas], [2, 3])

    def test_la_carga_descarta_lo_que_paso_la_retencion(self):
        h = cola_panel.ColaHistory(self.path, retention_days=1)
        h.path.parent.mkdir(parents=True, exist_ok=True)
        h.path.write_text(
            '{"at": "2026-08-01T00:00:00Z", "breached_ttf": 9}\n'
            '{"at": "2026-09-02T15:00:00Z", "breached_ttf": 63}\n'
        )
        self.assertEqual(h.load(now=AHORA), 1)
        self.assertEqual(h.samples[0]["breached_ttf"], 63)

    def test_la_ventana_recorta_por_horas(self):
        h = cola_panel.ColaHistory(self.path)
        h.samples = _muestras(5, cada_minutos=60)
        self.assertEqual(len(h.window(2.5, now=AHORA)), 3)

    def test_un_stamp_sin_zona_se_lee_como_utc(self):
        # Mismo criterio que el panel de tickets: asumir hora local correria el
        # reloj cinco horas.
        h = cola_panel.ColaHistory(self.path)
        h.path.parent.mkdir(parents=True, exist_ok=True)
        h.path.write_text('{"at": "2026-09-02T15:00:00", "breached_ttf": 63}\n')
        h.load(now=AHORA)
        self.assertEqual(h.samples[0]["at"].tzinfo, timezone.utc)


class BuildViewTest(unittest.TestCase):
    def test_serie_vacia_no_finge_datos(self):
        v = cola_panel.build_view([], now=AHORA)
        self.assertEqual(v["points"], 0)
        self.assertEqual(v["headline"], "Serie en blanco")
        self.assertIn("primera muestra", v["subline"])
        self.assertTrue(all(t["value"] is None for t in v["cards"]))
        self.assertTrue(all(t["delta"] is None for t in v["cards"]))

    def test_una_sola_muestra_da_valor_pero_no_tendencia(self):
        v = cola_panel.build_view(_muestras(1), now=AHORA)
        tarjeta = {t["key"]: t for t in v["cards"]}["breached_ttf"]
        self.assertEqual(tarjeta["value"], 60)
        self.assertIsNone(tarjeta["delta"])
        self.assertIn("una sola muestra", v["subline"])

    def test_el_delta_se_mide_contra_el_inicio_de_la_ventana(self):
        v = cola_panel.build_view(_muestras(4, paso_ttf=3), now=AHORA)
        tarjeta = {t["key"]: t for t in v["cards"]}["breached_ttf"]
        self.assertEqual(tarjeta["value"], 69)
        self.assertEqual(tarjeta["delta"], 9)

    def test_que_suban_los_vencidos_es_aviso(self):
        v = cola_panel.build_view(_muestras(3, paso_ttf=2), now=AHORA)
        self.assertEqual(v["severity"], "leve")

    def test_una_cola_quieta_no_alarma(self):
        v = cola_panel.build_view(_muestras(3), now=AHORA)
        self.assertEqual(v["severity"], "ok")

    def test_que_bajen_los_vencidos_tampoco_alarma(self):
        v = cola_panel.build_view(_muestras(3, ttf=60, paso_ttf=-4), now=AHORA)
        self.assertEqual(v["severity"], "ok")

    def test_una_serie_congelada_se_declara_parada(self):
        # Que el muestreador deje de anotar no es calma: sin esto la pantalla
        # mostraria la ultima curva como si fuera de ahora.
        viejas = [dict(m, at=m["at"] - timedelta(hours=2)) for m in _muestras(3)]
        v = cola_panel.build_view(viejas, now=AHORA, sample_seconds=300)
        self.assertTrue(v["stalled"])
        self.assertTrue(v["stale"])
        self.assertIn("dejo de anotar", v["subline"])

    def test_el_hero_lleva_los_activos_y_el_pie_los_sin_fecha(self):
        muestras = _muestras(2)
        muestras[-1]["no_deadline"] = 2
        v = cola_panel.build_view(muestras, now=AHORA)
        self.assertEqual(v["headline"], "90 tickets activos")
        self.assertEqual(v["no_deadline"], 2)

    def test_un_contador_que_el_agregado_no_trae_no_inventa_cero(self):
        # Si el flow deja de emitir una clave, la tarjeta dice "—" en vez de un
        # cero que se leeria como buena noticia.
        muestras = [{k: v for k, v in m.items() if k != "untriaged"}
                    for m in _muestras(3)]
        v = cola_panel.build_view(muestras, now=AHORA)
        tarjeta = {t["key"]: t for t in v["cards"]}["untriaged"]
        self.assertIsNone(tarjeta["value"])
        self.assertEqual(tarjeta["points"], 0)

    def test_las_muestras_desordenadas_se_ordenan(self):
        muestras = _muestras(3, paso_ttf=5)
        v = cola_panel.build_view(list(reversed(muestras)), now=AHORA)
        tarjeta = {t["key"]: t for t in v["cards"]}["breached_ttf"]
        self.assertEqual(tarjeta["delta"], 10)


class CurvaTest(unittest.TestCase):
    def test_la_escala_arranca_en_el_minimo_no_en_cero(self):
        # Con base cero un backlog que se mueve entre 60 y 65 se ve como una
        # linea recta, que es justo lo que el panel viene a contestar.
        c = cola_panel._curva([60, 65])
        ys = [float(p.split(",")[1]) for p in c["puntos"].split(" ")]
        self.assertGreater(ys[0], ys[1])          # 60 abajo, 65 arriba
        self.assertEqual((c["lo"], c["hi"]), (60, 65))

    def test_una_serie_plana_no_divide_por_cero(self):
        c = cola_panel._curva([7, 7, 7])
        self.assertEqual((c["lo"], c["hi"]), (7, 7))
        self.assertEqual(len(c["puntos"].split(" ")), 3)

    def test_el_ultimo_punto_cabe_entero_con_su_radio(self):
        # Pegado al borde, el SVG recorta medio punto y parece una marca a
        # medio pintar: paso de verdad en la primera captura.
        radio = 4
        c = cola_panel._curva([1, 50, 3])
        self.assertLessEqual(c["ultimo_x"] + radio, cola_panel.SPARK_W)
        self.assertGreaterEqual(c["ultimo_y"] - radio, 0)
        self.assertLessEqual(c["ultimo_y"] + radio, cola_panel.SPARK_H)

    def test_sin_datos_no_hay_curva(self):
        self.assertEqual(cola_panel._curva([])["puntos"], "")

    def test_el_submuestreo_conserva_la_ultima_muestra(self):
        # El ultimo punto lleva la etiqueta del valor de ahora: perderlo seria
        # pintar una curva que no termina donde dice el numero grande.
        muestras = _muestras(500, cada_minutos=1)
        recortadas = cola_panel._submuestrear(muestras, tope=50)
        self.assertEqual(len(recortadas), 50)
        self.assertIs(recortadas[-1], muestras[-1])


class MuestreadorTest(unittest.IsolatedAsyncioTestCase):
    """El lazo que anota. Sin este test, un fallo del cableado solo se ve en
    produccion y encima disfrazado: la pagina sirve igual, con la serie
    congelada."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.history = cola_panel.ColaHistory(
            Path(self.dir.name) / "data" / "cola-history.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    async def _una_vuelta(self, fuente):
        tarea = cola_panel.start_sampler_task(self.history, fuente, interval_seconds=3600)
        for _ in range(50):
            await asyncio.sleep(0)
            if self.history.samples:
                break
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        return tarea

    async def test_anota_lo_que_devuelve_la_fuente(self):
        async def fuente():
            return {"breached_ttf": 63, "active": 82}
        await self._una_vuelta(fuente)
        self.assertEqual(self.history.samples[-1]["breached_ttf"], 63)
        self.assertTrue(self.history.path.exists())

    async def test_una_fuente_que_no_es_corrutina_falla_ruidosamente(self):
        # Es el fallo que se colo: el decorador de lifespan quedo puesto en la
        # fuente, asi que 'await fuente()' reventaba con TypeError y la serie
        # se quedaba vacia sin que la pagina se viera mal.
        def fuente_mala():
            return {"breached_ttf": 63}
        tarea = cola_panel.start_sampler_task(self.history, fuente_mala, interval_seconds=3600)
        with self.assertLogs("quiosco.cola_panel", level="ERROR") as log:
            for _ in range(50):
                await asyncio.sleep(0)
                if log.output:
                    break
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        self.assertIn("TypeError", log.output[0])
        self.assertEqual(self.history.samples, [])

    async def test_sin_contadores_avisa_y_no_anota(self):
        async def fuente():
            return None
        tarea = cola_panel.start_sampler_task(self.history, fuente, interval_seconds=3600)
        with self.assertLogs("quiosco.cola_panel", level="WARNING") as log:
            for _ in range(50):
                await asyncio.sleep(0)
                if log.output:
                    break
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        self.assertEqual(self.history.samples, [])
        self.assertIn("no dio contadores", log.output[0])


class SettingsTest(unittest.TestCase):
    def test_defaults_y_pisos(self):
        s = cola_panel.panel_settings({})
        self.assertEqual(s["sample_seconds"], cola_panel.DEFAULT_SAMPLE_SECONDS)
        # Un sample_seconds de 5 s le pegaria al flow doce veces por minuto.
        s = cola_panel.panel_settings({"cola_panel": {"sample_seconds": 5}})
        self.assertEqual(s["sample_seconds"], 60)

    def test_bloque_basura_no_revienta(self):
        s = cola_panel.panel_settings({"cola_panel": "si"})
        self.assertEqual(s["window_hours"], cola_panel.DEFAULT_WINDOW_HOURS)

    def test_la_serie_vive_junto_al_config(self):
        # En produccion data/ es el volumen montado: si el archivo cayera
        # dentro de la imagen, la serie moriria en cada despliegue.
        p = cola_panel.history_path("/home/cip-exodia/quiosco/config.json")
        self.assertEqual(p, Path("/home/cip-exodia/quiosco/data/cola-history.jsonl"))


class VolumenTest(unittest.TestCase):
    """El aviso del volumen. Es el fallo que se ve normal: el panel pinta su
    curva y el despliegue siguiente se lleva la serie sin sintoma."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "data" / "cola-history.jsonl"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.dir.cleanup()

    def test_fuera_del_contenedor_no_avisa_nada(self):
        # En la laptop data/ nunca es un mount y avisar seria ruido diario.
        self.assertIsNone(
            cola_panel.aviso_si_no_hay_volumen(self.path, en_contenedor=False))

    def test_dentro_del_contenedor_y_sin_mount_grita(self):
        # La raiz de la app y data/ en el mismo dispositivo = no hay bind mount.
        with self.assertLogs("quiosco.cola_panel", level="ERROR") as log:
            aviso = cola_panel.aviso_si_no_hay_volumen(
                self.path, en_contenedor=True, raiz_app=Path(self.dir.name))
        self.assertIn("NO es un volumen montado", aviso)
        self.assertIn("./data:/app/data", log.output[0])

    def test_con_mount_de_verdad_se_queda_callado(self):
        # /proc siempre esta en otro dispositivo, asi que hace de doble de un
        # bind mount sin tener que montar nada en el test.
        self.assertIsNone(cola_panel.aviso_si_no_hay_volumen(
            self.path, en_contenedor=True, raiz_app=Path("/proc")))

    def test_una_raiz_que_no_existe_no_inventa_un_aviso(self):
        self.assertIsNone(cola_panel.aviso_si_no_hay_volumen(
            self.path, en_contenedor=True, raiz_app=Path("/no-existe-jamas")))


class RenderTest(unittest.TestCase):
    def test_html_lleva_datos_y_endpoint(self):
        v = cola_panel.build_view(_muestras(3), now=AHORA)
        html = cola_panel.render_html(v, poll_seconds=60)
        self.assertIn('id="datos"', html)
        self.assertIn("/api/cola-panel", html)
        self.assertIn("Cómo va la cola", html)

    def test_el_json_embebido_no_puede_cerrar_el_script(self):
        v = cola_panel.build_view([], now=AHORA)
        v["headline"] = "</script><script>alert(1)</script>"
        self.assertNotIn("<script>alert", cola_panel.render_html(v, poll_seconds=60))


class ContadoresDelCacheTest(unittest.TestCase):
    """last_counts() es la union entre el cache del ITSM y esta serie."""

    def _cache_con(self, counts):
        cache = itsm_panel.PanelCache()
        cache._payload = {"at_risk": [], "counts": counts}
        return cache

    def test_incluye_active_que_la_vista_del_otro_panel_no_propaga(self):
        c = self._cache_con({"breached_ttf": 63, "active": 82})
        self.assertEqual(c.last_counts(), {"breached_ttf": 63, "active": 82})

    def test_una_clave_desconocida_no_entra_a_la_serie(self):
        # Que el flow empiece a emitir algo nuevo no debe meterlo en la serie
        # sin que nadie haya decidido como se pinta.
        c = self._cache_con({"breached_ttf": 63, "inventado": 5})
        self.assertEqual(c.last_counts(), {"breached_ttf": 63})

    def test_no_deadline_si_llega_entra_solo(self):
        # Esta previsto que el flow lo añada; es la unica clave nueva ya
        # acordada, y el pie del panel la muestra.
        c = self._cache_con({"breached_ttf": 63, "no_deadline": 2})
        self.assertEqual(c.last_counts()["no_deadline"], 2)

    def test_valores_basura_se_saltan_sin_reventar(self):
        c = self._cache_con({"breached_ttf": "x", "active": 82})
        self.assertEqual(c.last_counts(), {"active": 82})

    def test_sin_agregado_todavia_no_hay_contadores(self):
        self.assertIsNone(itsm_panel.PanelCache().last_counts())


if __name__ == "__main__":
    unittest.main()
