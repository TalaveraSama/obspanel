"""Reproductor VLC simulado para pruebas (sin libvlc)."""
from engines.vlc_player import PlayerSnapshot


class FakePlayer:
    def __init__(self):
        self.uri = ""
        self.state = "stopped"
        self.pos = 0
        self.length = 0
        self.volume = 100
        self.calls = []          # ("open", uri, start_ms) ...
        self.audio_sel = -1
        self.sub_sel = -1
        self._end_cb = None

    def connect(self):
        return True, ""

    def close(self):
        self.uri = ""
        self.state = "stopped"

    def has_input(self):
        return bool(self.uri)

    def uri_now(self):
        return self.uri

    def position_ms(self):
        return int(self.pos)

    def length_ms(self):
        return int(self.length or 0)

    def open_uri(self, uri, start_ms=0, options=None, volume=100):
        self.uri = str(uri)
        self.state = "playing"
        self.pos = int(max(0, start_ms or 0))
        self.length = 600_000
        self.volume = int(volume or 100)
        self.calls.append(("open", str(uri), int(max(0, start_ms or 0))))
        return {"ok": True, "uri": str(uri)}

    def select_tracks(self, audio_index=-1, subtitle_index=-1, external_sub=None):
        self.audio_sel = int(audio_index or -1)
        self.sub_sel = int(subtitle_index if subtitle_index is not None else -1)
        return {"ok": True}

    def play(self):
        self.state = "playing"
        return {"ok": True}

    def pause(self):
        self.state = "paused"
        return {"ok": True}

    def stop(self):
        self.state = "stopped"
        return {"ok": True}

    def seek(self, ms):
        self.pos = int(max(0, ms or 0))
        return {"ok": True, "target_ms": int(ms or 0), "actual_ms": int(self.pos)}

    def set_volume(self, value):
        self.volume = max(0, min(200, int(value)))
        return {"ok": True, "volume": self.volume}

    def audio_tracks(self):
        return []

    def subtitle_tracks(self):
        return []

    def set_on_end(self, cb):
        self._end_cb = cb

    def snapshot(self):
        snap = PlayerSnapshot()
        snap.available = True
        snap.error = ""
        snap.state = self.state
        snap.uri = self.uri
        snap.position_ms = int(self.pos)
        snap.length_ms = int(self.length or 0)
        snap.volume = int(self.volume)
        snap.has_input = bool(self.uri)
        return snap
