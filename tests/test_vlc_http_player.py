"""Pruebas del controlador de la APLICACION VLC instalada (vía interfaz HTTP).

Se levanta un mini servidor que imita la API HTTP de VLC (status.xml), sin
necesidad de tener VLC instalado. Verifica que el player usa una ventana
persistente (solo ordena reproducir/parar/buscar), que es la base para que
OBS capture la ventana de VLC sin que cambie de handle.
"""
import base64
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines.vlc_player import VlcHttpPlayer  # noqa: E402


class FakeVLC:
    def __init__(self):
        self.state = "stopped"
        self.time_s = 0
        self.length_s = 0
        self.vol = 256
        self.current = ""

    def xml(self):
        return (f'<root><state>{self.state}</state><time>{self.time_s}</time>'
                f'<length>{self.length_s}</length><volume>{self.vol}</volume></root>')


class _Handler(BaseHTTPRequestHandler):
    fake = None
    password = "tvplayout"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        expect = "Basic " + base64.b64encode((":" + self.password).encode()).decode()
        if self.headers.get("Authorization", "") != expect:
            self.send_response(401)
            self.end_headers()
            return
        if not u.path.startswith("/requests/"):
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(u.query)
        cmd = (q.get("command") or [""])[0]
        f = self.fake
        if u.path == "/requests/status.xml":
            if cmd == "in_play":
                inp = (q.get("input") or [""])[0].split(" :")[0]
                f.current = unquote(inp).split("/")[-1]
                f.state = "playing"
                f.time_s = 0
                f.length_s = 600
            elif cmd == "seek":
                f.time_s = int(float((q.get("val") or [0])[0]))
            elif cmd == "pl_pause":
                f.state = "paused" if f.state == "playing" else "playing"
            elif cmd == "pl_play":
                f.state = "playing"
            elif cmd == "pl_stop":
                f.state = "stopped"
                f.current = ""
            elif cmd == "volume":
                f.vol = int(float((q.get("val") or [256])[0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(f.xml().encode())
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<root></root>")


class HttpPlayerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeVLC()
        _Handler.fake = self.fake
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.pl = VlcHttpPlayer({"vlc": {
            "mode": "app", "http_host": "127.0.0.1", "http_port": self.port,
            "http_password": "tvplayout", "fullscreen": False, "exe": "/none/vlc.exe"}})

    def tearDown(self):
        self.srv.shutdown()

    def test_connect_open_seek_controls(self):
        ok, err = self.pl.connect()
        self.assertTrue(ok, err)
        r = self.pl.open_uri("/tmp/Peli X.mkv", start_ms=60000)
        self.assertTrue(r.get("ok"), r)
        snap = self.pl.snapshot()
        self.assertEqual(snap.state, "playing")
        self.assertIn("Peli X.mkv", snap.uri)
        self.assertLess(abs(snap.position_ms - 60000), 3000)
        self.assertEqual(snap.length_ms, 600000)

    def test_pause_play_stop_volume(self):
        self.pl.connect()
        self.pl.open_uri("/tmp/X.mkv")
        self.pl.pause()
        self.assertEqual(self.pl.snapshot().state, "paused")
        self.pl.play()
        self.assertEqual(self.pl.snapshot().state, "playing")
        self.pl.set_volume(50)
        self.assertAlmostEqual(self.pl.snapshot().volume, 50, delta=2)
        self.pl.stop()
        snap = self.pl.snapshot()
        self.assertEqual(snap.state, "stopped")
        self.assertTrue(snap.movie_ended)


if __name__ == "__main__":
    unittest.main()
