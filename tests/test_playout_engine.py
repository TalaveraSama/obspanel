"""Pruebas del motor de playout VLC (sin libvlc, con FakePlayer)."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines.playout_engine import PlayoutEngine  # noqa: E402
from engines.vlc_player import (  # noqa: E402
    _pick_es_by_language,
    _pick_es_by_ordinal,
)
from engines.vlc_player import TrackDesc  # noqa: E402
from tests.fake_player import FakePlayer  # noqa: E402


SCHEMA = """
CREATE TABLE folders(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE,name TEXT,enabled INTEGER DEFAULT 1,category TEXT DEFAULT 'Movie',recursive INTEGER DEFAULT 1);
CREATE TABLE media(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE,title TEXT,duration REAL DEFAULT 0,width INTEGER DEFAULT 0,height INTEGER DEFAULT 0,audio_json TEXT DEFAULT '[]',subs_json TEXT DEFAULT '[]',category TEXT DEFAULT 'Movie',enabled INTEGER DEFAULT 1,folder_id INTEGER,size INTEGER DEFAULT 0,mtime REAL DEFAULT 0);
CREATE TABLE schedule(id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER,start_at TEXT,end_at TEXT,audio_index INTEGER DEFAULT 0,subtitle_index INTEGER DEFAULT -1,kind TEXT DEFAULT 'PROGRAM',enabled INTEGER DEFAULT 1,status TEXT DEFAULT 'scheduled',source TEXT DEFAULT 'MANUAL',day_key TEXT DEFAULT '',generated_run TEXT DEFAULT '');
CREATE TABLE asrun(id INTEGER PRIMARY KEY AUTOINCREMENT,event_time TEXT,media_id INTEGER,title TEXT,kind TEXT,audio_index INTEGER DEFAULT 0,subtitle_index INTEGER DEFAULT -1,duration REAL DEFAULT 0,status TEXT DEFAULT 'PLAYED');
"""


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tvplayout_test_")
        self.db = os.path.join(self.tmp, "test.db")
        c = sqlite3.connect(self.db)
        c.executescript(SCHEMA)
        c.commit()
        c.close()
        # archivos reales para que el motor acepte las rutas
        self.dir = Path(self.tmp) / "media"
        self.dir.mkdir()
        self.mov_a = self.dir / "Pelicula A.mkv"
        self.mov_b = self.dir / "Pelicula B.mkv"
        self.ad1 = self.dir / "Spot 1.mkv"
        self.mov_a.write_bytes(b"x")
        self.mov_b.write_bytes(b"x")
        self.ad1.write_bytes(b"x")
        self.player = FakePlayer()
        self.now = datetime(2026, 9, 3, 12, 0, 0)
        self.engine = PlayoutEngine(
            cfg={"vlc": {"volume": 100}, "auto_ads": {"enabled": False}},
            db_path=self.db,
            player=self.player,
            base_dir=Path(self.tmp),
            clock=lambda: self.now,
        )

    def _add_media(self, path, title, duration_s, category="Movie"):
        c = sqlite3.connect(self.db)
        c.execute(
            "INSERT INTO media(path,title,duration,category,enabled,audio_json,subs_json) VALUES(?,?,?,?,1,'[]','[]')",
            (str(path), title, duration_s, category),
        )
        c.commit()
        mid = c.execute("SELECT id FROM media WHERE path=?", (str(path),)).fetchone()[0]
        c.close()
        return mid

    def _add_event(self, mid, start, end, kind="PROGRAM", status="scheduled", source="MANUAL"):
        c = sqlite3.connect(self.db)
        c.execute(
            "INSERT INTO schedule(media_id,start_at,end_at,kind,status,source) VALUES(?,?,?,?,?,?)",
            (mid, start.strftime("%Y-%m-%dT%H:%M:%S"),
             end.strftime("%Y-%m-%dT%H:%M:%S"), kind, status, source),
        )
        c.commit()
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.close()
        return sid

    def _rows(self, table="schedule"):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        r = [dict(x) for x in c.execute(f"SELECT * FROM {table} ORDER BY id")]
        c.close()
        return r

    def test_next_movie_changes_when_time_arrives(self):
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=10))
        self._add_event(mb, start + timedelta(minutes=10), start + timedelta(minutes=20))
        self.player.pos = 0
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a))

        # avanzamos el reloj al inicio de la segunda película
        self.now = start + timedelta(minutes=10, seconds=1)
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "Al llegar la hora debe cargar la siguiente película")
        self.assertTrue(any(c[0] == "open" and c[1] == str(self.mov_b) for c in self.player.calls))

    def test_ad_interrupts_and_resumes_at_real_position(self):
        ma = self._add_media(self.mov_a, "Película A", 1200)
        ad = self._add_media(self.ad1, "Spot", 30, category="Commercial")
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=20))            # película
        self._add_event(ad, start + timedelta(minutes=10),
                        start + timedelta(minutes=10, seconds=30),          # tanda
                        kind="COMMERCIAL", source="AUTO_ADS")
        self.player.pos = 0
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a))

        # llega la tanda: VLC está en el minuto 10:00 (600000 ms)
        self.now = start + timedelta(minutes=10)
        self.player.pos = 600_000
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.ad1),
                         "La tanda debe cortar la película en VLC")
        self.assertTrue(self.engine.ui["ad_break"])
        self.assertIsNotNone(self.engine._interrupt)
        self.assertEqual(self.engine._interrupt["position_ms"], 600_000)

        # termina la tanda: la película se reanuda en 600000 ms (no 630000)
        self.now = start + timedelta(minutes=10, seconds=31)
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a),
                         "Tras la tanda debe volver la película")
        self.assertGreaterEqual(self.player.pos, 600_000)
        self.assertLess(self.player.pos, 601_000)
        self.assertFalse(self.engine.ui["ad_break"])

    def test_reload_when_player_empty(self):
        ma = self._add_media(self.mov_a, "Película A", 1200)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=20))
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a))
        # se cae VLC
        self.player.uri = ""
        self.player.state = "stopped"
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a),
                         "El motor debe recargar solo el evento en curso")

    def test_cut_now_inserts_ad_events(self):
        ma = self._add_media(self.mov_a, "Película A", 1200)
        self._add_media(self.ad1, "Spot 1", 30, category="Commercial")
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=20))
        self.engine.tick()
        res = self.engine.ad_cut_now()
        self.assertTrue(res.get("ok"), res)
        rows = self._rows()
        kinds = [r["kind"] for r in rows if r["source"] == "AUTO_ADS"]
        self.assertTrue(kinds and all(k == "COMMERCIAL" for k in kinds),
                        "cut-now debe insertar eventos COMMERCIAL AUTO_ADS")
        # el motor pasa a la tanda cuando el reloj la alcanza
        first_ad = next(r for r in rows if r["source"] == "AUTO_ADS")
        self.now = datetime.fromisoformat(first_ad["start_at"])
        self.player.pos = 30_000
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.ad1))

    def test_take_plays_media_without_schedule(self):
        ma = self._add_media(self.mov_a, "Película A", 600)
        row = {"id": ma, "path": str(self.mov_a), "title": "Película A",
               "duration": 600.0, "audio_json": "[]", "subs_json": "[]"}
        res = self.engine.take(row)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.player.uri, str(self.mov_a))

    def test_take_survives_ticks_until_next_event(self):
        """TOMAR no debe ser pisado por el Scheduler hasta el próximo evento."""
        mov_c = self.dir / "Pelicula C.mkv"
        mov_c.write_bytes(b"x")
        mc = self._add_media(mov_c, "Película C", 600)
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=10))
        self._add_event(mb, start + timedelta(minutes=10), start + timedelta(minutes=20))

        take_row = {"id": mc, "path": str(mov_c), "title": "Película C",
                    "duration": 600.0, "audio_json": "[]", "subs_json": "[]"}
        res = self.engine.take(take_row)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.player.uri, str(mov_c))

        # Varios ticks durante la ventana de la toma: sigue sonando la toma
        self.now = start + timedelta(minutes=1)
        self.engine.tick()
        self.now = start + timedelta(minutes=5)
        self.engine.tick()
        self.assertEqual(self.player.uri, str(mov_c),
                         "La toma debe continuar al aire pese al Scheduler")

        # Al llegar el siguiente evento programado, el Scheduler recupera el canal
        self.now = start + timedelta(minutes=10, seconds=1)
        self.player.uri = str(mov_c)  # toma aún sonando
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "Al vencer la toma debe retomar el evento programado")

    def test_marks_played_when_window_ends(self):
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=10))
        self._add_event(mb, start + timedelta(minutes=10), start + timedelta(minutes=20))
        self.engine.tick()
        rows = self._rows()
        st = {r["id"]: r["status"] for r in rows}
        self.assertEqual(st.get(1), "playing")

        self.now = start + timedelta(minutes=10, seconds=1)
        self.engine.tick()
        rows = self._rows()
        st = {r["id"]: r["status"] for r in rows}
        self.assertEqual(st.get(1), "played", "El evento vencido debe pasar a played")
        self.assertEqual(st.get(2), "playing")

    def test_ad_does_not_mark_program_played(self):
        ma = self._add_media(self.mov_a, "Película A", 1200)
        ad = self._add_media(self.ad1, "Spot", 30, category="Commercial")
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=20))
        self._add_event(ad, start + timedelta(minutes=10),
                        start + timedelta(minutes=10, seconds=30),
                        kind="COMMERCIAL", source="AUTO_ADS")
        self.engine.tick()
        self.now = start + timedelta(minutes=10)
        self.player.pos = 600_000
        self.engine.tick()
        rows = self._rows()
        st = {r["id"]: r["status"] for r in rows}
        self.assertEqual(st.get(1), "playing",
                         "La tanda no debe marcar played la película (aún se reanuda)")

    def test_no_reload_loop_when_uri_is_file_url(self):
        """libvlc reporta file:///C:/... y el Scheduler guarda C:\\...: no debe
        recargar en bucle (era la causa de que VLC se abriera y cerrara)."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=10))
        self.engine.tick()
        opens = [c for c in self.player.calls if c[0] == "open"]
        self.assertEqual(len(opens), 1, "la película debe cargarse una sola vez")

        # VLC devuelve el archivo como file:/// con %20 en lugar de ruta plana
        self.player.uri = Path(self.mov_a).resolve().as_uri()
        self.player.state = "playing"
        self.player.pos = 42_000
        for _ in range(3):
            self.engine.tick()
        opens = [c for c in self.player.calls if c[0] == "open"]
        self.assertEqual(len(opens), 1,
                         "file:/// y ruta local son el mismo archivo: no recargar")


    def test_action_next_plays_immediately_and_holds_until_scheduled(self):
        """⏭ SIGUIENTE con la peli B futura: debe sonar YA y NO detenerse en
        los ticks siguientes (antes VLC paraba -> pantalla negra)."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=30))
        self._add_event(mb, start + timedelta(minutes=30), start + timedelta(minutes=60))
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a))

        res = self.engine.action_next()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "SIGUIENTE debe cargar la siguiente película al instante")

        # Pasan ticks: la toma sostiene B; el Scheduler NO debe pararla.
        for _ in range(5):
            self.now += timedelta(seconds=1)
            self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "La película adelantada debe seguir al aire (sin negro)")
        self.assertEqual(self.player.state, "playing")

        # Al llegar la hora de B la entrega es sin corte: no se recarga.
        opens_before = len([c for c in self.player.calls if c[0] == "open"])
        self.now = start + timedelta(minutes=30, seconds=1)
        self.player.uri = str(self.mov_b)
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b))
        opens_after = len([c for c in self.player.calls if c[0] == "open"])
        self.assertEqual(opens_before, opens_after,
                         "La entrega al Scheduler no debe recargar la misma película")

    def test_action_next_skips_pending_ads_between(self):
        """SIGUIENTE pide la siguiente PELÍCULA: las tandas intermedias se
        marcan emitidas y no deben interrumpir la película adelantada."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        ad = self._add_media(self.ad1, "Spot", 30, category="Commercial")
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=30))
        self._add_event(ad, start + timedelta(minutes=10),
                        start + timedelta(minutes=10, seconds=30),
                        kind="COMMERCIAL", source="AUTO_ADS")
        self._add_event(mb, start + timedelta(minutes=30), start + timedelta(minutes=60))
        self.engine.tick()
        self.engine.action_next()
        self.assertEqual(self.player.uri, str(self.mov_b))

        # Llega la hora de la tanda intermedia: NO debe cortar la película B.
        self.now = start + timedelta(minutes=10, seconds=1)
        self.player.pos = 100_000
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "La tanda intermedia saltada no debe interrumpir")
        self.assertFalse(self.engine.ui["ad_break"])

    def test_ended_movie_does_not_flicker_at_window_edge(self):
        """Película terminada justo al filo del cambio: no se recarga (antes
        VLC parpadeaba en negro recargando el mismo archivo)."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        self._add_event(ma, start, start + timedelta(seconds=600))
        self._add_event(mb, start + timedelta(seconds=600), start + timedelta(seconds=1200))
        self.engine.tick()
        # A terminó su archivo a 1s del cambio de evento.
        self.now = start + timedelta(seconds=599)
        self.player.state = "ended"
        self.player.pos = 600_000
        self.engine.tick()
        opens = [c for c in self.player.calls if c[0] == "open"]
        self.assertEqual(len(opens), 1,
                         "No debe recargar una película que ya terminó")

    def test_ended_movie_advances_to_next_early(self):
        """Si la película termina mucho antes de su ventana, el motor encadena
        con la siguiente película en vez de dejar VLC parado."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        # Ventana de A larga; su archivo (FakePlayer) "termina" de inmediato.
        self._add_event(ma, start, start + timedelta(minutes=30))
        self._add_event(mb, start + timedelta(minutes=30), start + timedelta(minutes=60))
        self.engine.tick()
        self.now = start + timedelta(minutes=5)
        self.player.state = "ended"
        self.player.pos = 600_000
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b),
                         "Al terminar la película con tiempo de sobra, encadena la siguiente")

    def test_ad_skip_only_clears_current_break(self):
        """SALTAR TANDA no debe borrar tandas futuras lejanas (antes borraba
        todos los AUTO_ADS de los siguientes 30 minutos)."""
        ma = self._add_media(self.mov_a, "Película A", 7200)
        ad = self._add_media(self.ad1, "Spot", 30, category="Commercial")
        start = self.now
        self._add_event(ma, start, start + timedelta(minutes=120))
        # Tanda en curso (dos spots contiguos)
        self._add_event(ad, start, start + timedelta(seconds=30),
                        kind="COMMERCIAL", source="AUTO_ADS")
        self._add_event(ad, start + timedelta(seconds=30), start + timedelta(seconds=60),
                        kind="COMMERCIAL", source="AUTO_ADS")
        # Tanda futura (a los 40 min) que DEBE conservarse
        future = self._add_event(ad, start + timedelta(minutes=40),
                                 start + timedelta(minutes=40, seconds=30),
                                 kind="COMMERCIAL", source="AUTO_ADS")
        self.player.pos = 30_000
        self.now = start + timedelta(seconds=1)
        self.engine.tick()  # entra la tanda
        self.assertTrue(self.engine.ui["ad_break"])
        res = self.engine.ad_skip()
        self.assertTrue(res.get("ok"), res)
        rows = {r["id"]: r["status"] for r in self._rows()}
        self.assertEqual(rows.get(future), "scheduled",
                         "La tanda futura debe seguir programada")

    def test_action_previous_plays_immediately(self):
        """⏮ ANTERIOR también reproduce ya (mismo arreglo que SIGUIENTE)."""
        ma = self._add_media(self.mov_a, "Película A", 600)
        mb = self._add_media(self.mov_b, "Película B", 600)
        start = self.now
        # A ya pasó (ventana cerrada), B está al aire ahora.
        self._add_event(ma, start - timedelta(minutes=30), start - timedelta(seconds=1))
        self._add_event(mb, start, start + timedelta(minutes=30))
        self.now = start + timedelta(minutes=1)
        self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_b))
        res = self.engine.action_previous()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.player.uri, str(self.mov_a))
        for _ in range(3):
            self.now += timedelta(seconds=1)
            self.engine.tick()
        self.assertEqual(self.player.uri, str(self.mov_a),
                         "La película anterior debe sostenerse al aire")


class TrackMappingTest(unittest.TestCase):
    def test_ordinal_ignores_disable_entry(self):
        descs = [TrackDesc(-1, "Disable"), TrackDesc(4, "Spanish"),
                 TrackDesc(5, "English")]
        self.assertEqual(_pick_es_by_ordinal(descs, 0), 4)
        self.assertEqual(_pick_es_by_ordinal(descs, 1), 5)

    def test_language_match(self):
        descs = [TrackDesc(-1, "Disable"), TrackDesc(3, "es - Spanish"),
                 TrackDesc(7, "eng - English")]
        self.assertEqual(_pick_es_by_language(descs, "es"), 3)
        self.assertEqual(_pick_es_by_language(descs, "en"), 7)


if __name__ == "__main__":
    unittest.main()
