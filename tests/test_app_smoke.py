"""Pruebas de integración de la app FastAPI (sin libvlc real).

- AppSmokeTest: la app arranca y sirve UI/API en modo degradado.
- AppE2ETest: con un reproductor falso inyectado, recorre el flujo real del
  usuario: SIGUIENTE PELÍCULA automático y tanda comercial (cut-now/skip).

Todas usan una base SQLite temporal; nunca tocan tvplayout.db del usuario.
"""
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from fastapi.testclient import TestClient  # noqa: F401
except Exception:  # pragma: no cover
    TestClient = None

try:
    import app_backend  # noqa: E402
    from tests.fake_player import FakePlayer  # noqa: E402
except Exception:  # pragma: no cover - entorno sin fastapi
    app_backend = None
    FakePlayer = None

STAMP = "%Y-%m-%dT%H:%M:%S"


def _wait_until(client, cond, timeout=8.0, step=0.4):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = cond(client)
        if last:
            return last
        time.sleep(step)
    return last


@unittest.skipIf(TestClient is None or app_backend is None,
                 "fastapi/httpx no instalado")
class AppSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_db = app_backend.DB
        cls.tmp = Path(tempfile.mkdtemp(prefix="tvplayout_smoke_"))
        cls.db = cls.tmp / "smoke.db"
        app_backend.DB = cls.db
        app_backend.PLAYER = None
        app_backend.ENGINE = None
        app_backend.init_db()

    @classmethod
    def tearDownClass(cls):
        app_backend.PLAYER = None
        app_backend.ENGINE = None
        app_backend.DB = cls._old_db

    def test_home_and_api(self):
        with TestClient(app_backend.app) as client:
            r = client.get("/?tab=playout")
            self.assertEqual(r.status_code, 200)
            self.assertIn("TVPlayout", r.text)
            self.assertIn("VLC", r.text)

            r = client.get("/api/status")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["ok"])
            self.assertIn("vlc", data)
            self.assertIn("state", data)
            self.assertIn("mode", data["state"])

            r = client.get("/api/vlc/info")
            self.assertEqual(r.status_code, 200)
            info = r.json()
            self.assertIn("module_available", info)
            self.assertIn("lib_dir", info)

            r = client.get("/api/vlc/status")
            self.assertEqual(r.status_code, 200)
            st = r.json()
            self.assertEqual(st["source"], "VLC")
            self.assertFalse(st["ready"])  # sin libvlc no puede estar listo

            r = client.get("/api/library")
            self.assertEqual(r.status_code, 200)
            lib = r.json()
            self.assertIsInstance(lib["items"], list)

            r = client.get("/?tab=scheduler")
            self.assertEqual(r.status_code, 200)

            r = client.post("/api/vlc/action", data={"action": "next"})
            self.assertIn(r.status_code, (409, 500))

            r = client.get("/api/obs")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["connected"])


@unittest.skipIf(TestClient is None or app_backend is None or FakePlayer is None,
                 "fastapi/httpx no instalado")
class AppE2ETest(unittest.TestCase):
    """Flujo completo del operador con reproductor falso inyectado."""

    @classmethod
    def setUpClass(cls):
        cls._old_db = app_backend.DB
        cls._old_build = app_backend.build_player
        cls.tmp = Path(tempfile.mkdtemp(prefix="tvplayout_e2e_"))
        mdir = cls.tmp / "media"
        mdir.mkdir()
        files = {}
        for name, title, cat, dur in (
            ("Peli Uno.mkv", "PELI UNO", "Movie", 40.0),
            ("Peli Dos.mkv", "PELI DOS", "Movie", 40.0),
            ("Spot Zeta.mkv", "SPOT ZETA", "Commercial", 8.0),
        ):
            p = mdir / name
            p.write_bytes(b"x")
            files[title] = (p, dur, cat)

        cls.db = cls.tmp / "e2e.db"
        app_backend.DB = cls.db
        app_backend.PLAYER = None
        app_backend.ENGINE = None
        app_backend.init_db()

        c = app_backend.db()
        ids = {}
        for title, (p, dur, cat) in files.items():
            cur = c.execute(
                "INSERT INTO media(path,title,duration,category,enabled,audio_json,subs_json)"
                " VALUES(?,?,?,?,1,'[]','[]')",
                (str(p), title, dur, cat))
            ids[title] = cur.lastrowid

        now = datetime.now()
        start_uno = now - timedelta(seconds=3)
        cls.next_at = now + timedelta(seconds=6)
        end_uno = start_uno + timedelta(seconds=50)
        end_dos = cls.next_at + timedelta(seconds=50)
        s = lambda dt: dt.strftime(STAMP)  # noqa: E731
        c.execute(
            "INSERT INTO schedule(media_id,start_at,end_at,audio_index,subtitle_index,kind,status,source,day_key)"
            " VALUES(?,?,?,0,-1,'PROGRAM','scheduled','AUTO_WEEKLY',?)",
            (ids["PELI UNO"], s(start_uno), s(end_uno), start_uno.strftime("%Y-%m-%d")))
        c.execute(
            "INSERT INTO schedule(media_id,start_at,end_at,audio_index,subtitle_index,kind,status,source,day_key)"
            " VALUES(?,?,?,0,-1,'PROGRAM','scheduled','AUTO_WEEKLY',?)",
            (ids["PELI DOS"], s(cls.next_at), s(end_dos), cls.next_at.strftime("%Y-%m-%d")))
        c.commit()
        c.close()

        cls.player = FakePlayer()
        app_backend.build_player = lambda cfg: cls.player

    @classmethod
    def tearDownClass(cls):
        app_backend.build_player = cls._old_build
        app_backend.PLAYER = None
        app_backend.ENGINE = None
        app_backend.DB = cls._old_db

    def _playout(self, client):
        return client.get("/api/playout").json()

    def test_siguiente_pelicula_y_tanda(self):
        with TestClient(app_backend.app) as client:
            uno = str(self.tmp / "media" / "Peli Uno.mkv")
            dos = str(self.tmp / "media" / "Peli Dos.mkv")

            # 1) PELI UNO al aire (el motor la carga al arrancar)
            ok = _wait_until(client,
                             lambda cl: (self._playout(cl).get("current") or {}).get("title") == "PELI UNO"
                             and self._playout(cl).get("vlc", {}).get("ready")
                             and self.player.uri == uno)
            self.assertTrue(ok, "No arrancó PELI UNO")

            # 2) SIGUIENTE PELÍCULA: al llegar la hora el motor cambia solo
            ok = _wait_until(client,
                             lambda cl: (self._playout(cl).get("current") or {}).get("title") == "PELI DOS"
                             and self.player.uri == dos,
                             timeout=12.0)
            self.assertTrue(ok, "No se cargó la siguiente película al llegar su hora")

            # 3) CORTE COMERCIAL inmediato
            r = client.post("/api/ads/cut-now")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertTrue(data.get("ok"), data)
            self.assertGreater(data.get("ads", 0), 0)

            spot = str(self.tmp / "media" / "Spot Zeta.mkv")
            ok = _wait_until(client,
                             lambda cl: bool(self._playout(cl).get("ad_break"))
                             and (self._playout(cl).get("current") or {}).get("title") == "SPOT ZETA"
                             and self.player.uri == spot,
                             timeout=8.0)
            self.assertTrue(ok, "La tanda no entró al aire")

            # 4) SALTAR la tanda: vuelve la película
            r = client.post("/api/ads/skip")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("ok"), r.text)

            ok = _wait_until(client,
                             lambda cl: not bool(self._playout(cl).get("ad_break"))
                             and (self._playout(cl).get("current") or {}).get("title") == "PELI DOS"
                             and self.player.uri == dos,
                             timeout=8.0)
            self.assertTrue(ok, "Tras saltar la tanda no volvió la película")


if __name__ == "__main__":
    unittest.main()
