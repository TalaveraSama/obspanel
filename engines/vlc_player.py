"""
TVPlayout — Motor VLC directo (libvlc via python-vlc).

Reemplaza por completo el camino anterior "panel -> OBS WebSocket -> fuente
VLC de OBS". Ahora el panel abre una instancia real de VLC (libvlc) en la
misma máquina y la controla directamente:

  - Reproduce el archivo original de cada evento programado.
  - Permite reanudar en el punto exacto (milisegundos) tras una tanda.
  - Selecciona pista de audio / subtítulo (internos o SRT externo).
  - Detecta el final del archivo (callback MediaPlayerEndReached).

Si python-vlc o libvlc no están disponibles el panel sigue funcionando;
el motor queda en estado "VLC no disponible" sin romper el resto.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Carga opcional de python-vlc
# ---------------------------------------------------------------------------
try:
    import vlc as _vlc

    VLC_BINDINGS_AVAILABLE = True
    VLC_BINDINGS_ERROR = ""
except Exception as _e:  # pragma: no cover - depende del entorno
    _vlc = None
    VLC_BINDINGS_AVAILABLE = False
    VLC_BINDINGS_ERROR = f"python-vlc no disponible: {_e}"


def _candidate_lib_dirs(cfg: dict) -> List[str]:
    """Rutas típicas donde puede estar la librería libvlc."""
    dirs: List[str] = []
    v = (cfg or {}).get("vlc") or {}
    for key in ("lib_dir", "path"):
        raw = str(v.get(key) or "").strip()
        if raw:
            p = Path(raw)
            dirs.append(str(p if p.is_dir() else p.parent))
    if sys.platform.startswith("win"):
        env = os.environ
        roots = [
            env.get("ProgramFiles", r"C:\Program Files"),
            env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            env.get("LOCALAPPDATA", ""),
        ]
        for r in roots:
            if r:
                dirs.append(str(Path(r) / "VideoLAN" / "VLC"))
        dirs.append(r"C:\VideoLAN\VLC")
        dirs.append(r"C:\VLC")
    elif sys.platform == "darwin":
        dirs.append("/Applications/VLC.app/Contents/MacOS/lib")
        dirs.append("/Applications/VLC.app/Contents/MacOS")
    else:
        dirs += [
            "/usr/lib/x86_64-linux-gnu/vlc",
            "/usr/lib/aarch64-linux-gnu/vlc",
            "/usr/lib/i386-linux-gnu/vlc",
            "/usr/lib64/vlc",
            "/usr/lib/vlc",
            "/snap/vlc/current/usr/lib/vlc",
            "/usr/local/lib/vlc",
        ]
    seen: List[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    return seen


def _find_libvlc_file(dirs: List[str]) -> str:
    names = {
        "win32": ["libvlc.dll", "libvlccore.dll"],
        "darwin": ["libvlc.dylib"],
        "linux": ["libvlc.so", "libvlc.so.5"],
    }.get(sys.platform, ["libvlc.so"])
    for d in dirs:
        for n in names:
            cand = Path(d) / n
            if cand.exists():
                return str(cand)
    # Fallback: python-vlc buscará por su cuenta (registro de Windows, etc.)
    return ""


def resolve_vlc(cfg: dict) -> dict:
    """Intenta localizar libvlc y devuelve un resumen utilizable por la UI."""
    result = {
        "ok": False,
        "error": "",
        "lib_dir": "",
        "lib_file": "",
        "module_available": VLC_BINDINGS_AVAILABLE,
        "vlc_version": "",
    }
    if not VLC_BINDINGS_AVAILABLE:
        result["error"] = VLC_BINDINGS_ERROR
        return result
    dirs = _candidate_lib_dirs(cfg)
    found = _find_libvlc_file(dirs)
    if found:
        result["lib_dir"] = str(Path(found).parent)
        result["lib_file"] = found
    # Si no encontramos nada, python-vlc aún puede localizarla vía registro
    # (Windows con VLC instalado). Le indicamos las carpetas candidatas.
    if result["lib_dir"]:
        os.environ["PYTHON_VLC_MODULE_PATH"] = result["lib_dir"]
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Estado normalizado
# ---------------------------------------------------------------------------
STATE_IDLE = "idle"
STATE_OPENING = "opening"
STATE_BUFFERING = "buffering"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_ENDED = "ended"
STATE_ERROR = "error"


@dataclass
class PlayerSnapshot:
    """Estado de un vistazo del reproductor, para la UI y el motor."""

    available: bool = False
    error: str = ""
    state: str = STATE_IDLE
    uri: str = ""
    position_ms: int = 0
    length_ms: int = 0
    volume: int = 100
    movie_ended: bool = False
    track_audio: int = -1
    track_sub: int = -1
    has_input: bool = False

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["playing"] = self.state == STATE_PLAYING
        d["position_seconds"] = max(0, self.position_ms) // 1000
        d["length_seconds"] = max(0, self.length_ms) // 1000
        return d


class TrackDesc:
    __slots__ = ("es_id", "name")

    def __init__(self, es_id: int, name: str = ""):
        self.es_id = int(es_id)
        self.name = str(name or "")

    def __repr__(self):  # pragma: no cover
        return f"<Track {self.es_id} {self.name!r}>"


# ---------------------------------------------------------------------------
# Interfaz que usa el motor de playout
# ---------------------------------------------------------------------------
class BasePlayer:
    """Contrato mínimo del reproductor (VLC real o fake para pruebas)."""

    def connect(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> PlayerSnapshot:
        raise NotImplementedError

    def has_input(self) -> bool:
        raise NotImplementedError

    def uri_now(self) -> str:
        raise NotImplementedError

    def position_ms(self) -> int:
        raise NotImplementedError

    def length_ms(self) -> int:
        raise NotImplementedError

    def open_uri(self, uri: str, start_ms: int = 0, options: Optional[List[str]] = None,
                 volume: int = 100) -> dict:
        raise NotImplementedError

    def select_tracks(self, audio_index: int = -1, subtitle_index: int = -1,
                      external_sub: Optional[str] = None) -> dict:
        raise NotImplementedError

    def play(self) -> dict:
        raise NotImplementedError

    def pause(self) -> dict:
        raise NotImplementedError

    def stop(self) -> dict:
        raise NotImplementedError

    def seek(self, ms: int) -> dict:
        raise NotImplementedError

    def set_volume(self, value: int) -> dict:
        raise NotImplementedError

    def audio_tracks(self) -> List[TrackDesc]:
        raise NotImplementedError

    def subtitle_tracks(self) -> List[TrackDesc]:
        raise NotImplementedError

    def set_on_end(self, callback: Optional[Callable[[], None]]) -> None:
        raise NotImplementedError


def _pick_es_by_ordinal(descriptions: List[TrackDesc], ordinal: int) -> Optional[int]:
    """Mapea un ordinal (0=primera pista) sobre la lista devuelta por libvlc.

    Las listas de libvlc pueden incluir entradas especiales (p. ej. id -1
    "Disable" en subtítulos); solo se cuentan pistas con id >= 0.
    """
    if ordinal is None or ordinal < 0:
        return None
    real = [t for t in descriptions if t.es_id >= 0]
    if not real:
        return None
    idx = min(int(ordinal), len(real) - 1)
    return real[idx].es_id


# Alias por idioma (códigos ISO y nombres comunes con/sin acentos).
_LANG_ALIASES: Dict[str, set] = {
    "es": {"es", "spa", "sp", "spanish", "castellano", "español", "espanol",
           "latino", "latinoamerica", "latinoamérica", "espania", "españa"},
    "en": {"en", "eng", "english", "inglés", "ingles", "ing"},
    "fr": {"fr", "fra", "fre", "french", "français", "frances", "francés"},
    "de": {"de", "ger", "deu", "german", "alemán", "aleman", "deutsch"},
    "pt": {"pt", "por", "portuguese", "portugués", "portugues", "português"},
    "it": {"it", "ita", "italian", "italiano"},
}


def _normalize_lang_code(lang: str) -> str:
    """Reduce un código/palabra de idioma a su clave canónica ('es','en',...)."""
    tok = str(lang or "").strip().lower()
    if not tok:
        return ""
    # "es-ES", "es_ES", "español", "spa"...
    bare = re.split(r"[-_]", tok)[0]
    for canon, aliases in _LANG_ALIASES.items():
        if bare in aliases or tok in aliases:
            return canon
    # match por prefijo 2 letras de códigos iso tipo "eng"
    for canon, aliases in _LANG_ALIASES.items():
        for a in aliases:
            if len(a) == 2 and bare.startswith(a):
                return canon
    return ""


def _pick_es_by_language(descriptions: List[TrackDesc], language: str,
                         wanted_title: str = "") -> Optional[int]:
    """Encuentra la pista que coincide con el idioma detectado por ffprobe.

    Empareja contra el nombre normalizado de cada pista usando alias de
    idioma, no subcadenas sueltas (evita falsos positivos como "es" dentro
    de "portuguese").
    """
    title = str(wanted_title or "").strip().lower()
    canon = _normalize_lang_code(language or "")
    if not canon and not title:
        return None
    aliases = _LANG_ALIASES.get(canon, set()) | ({canon} if canon else set())
    word = r"[^\W_]+"  # palabras unicode (incluye acentos)
    title_tokens = set(re.findall(word, title))
    for t in descriptions:
        if t.es_id < 0:
            continue
        name_tokens = set(re.findall(word, t.name.lower()))
        # El nombre puede ir prefijado por el código ('spa - Spanish'),
        # por tanto las primeras 2-3 letras ya se tienen en cuenta.
        if canon and (name_tokens & aliases):
            return t.es_id
        if title_tokens and (name_tokens & title_tokens):
            return t.es_id
    return None


# ---------------------------------------------------------------------------
# Reproductor real (python-vlc / libvlc)
# ---------------------------------------------------------------------------
class VlcPlayer(BasePlayer):
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self._vlc = _vlc
        self._instance = None
        self._player = None
        self._media = None
        self._connected = False
        self._last_error = ""
        self._volume = 100
        self._lock = threading.RLock()
        self._on_end: Optional[Callable[[], None]] = None
        self._end_pending = False
        # Seguimiento de lo que el motor cree que debe estar al aire
        self._expected_uri = ""
        self._expected_audio = -1
        self._expected_sub = -1
        self._expected_external_sub = None
        self._version = ""

    # ------------------------------------------------------------------ vida
    def connect(self) -> Tuple[bool, str]:
        with self._lock:
            if self._connected and self._player is not None:
                return True, ""
            if not VLC_BINDINGS_AVAILABLE:
                self._last_error = VLC_BINDINGS_ERROR or "python-vlc no instalado"
                return False, self._last_error
            try:
                resolve_vlc(self.cfg)
                v = (self.cfg.get("vlc") or {})
                args = [
                    "--quiet",
                    "--no-video-title-show",
                    "--no-keyboard-events",
                    "--no-mouse-events",
                    "--no-osd",
                    "--network-caching=%d" % int(v.get("network_caching", 300) or 300),
                    "--audio-language=%s" % (v.get("audio_language") or "es,en,spa"),
                    "--sub-language=%s" % (v.get("sub_language") or "es,en,spa"),
                ]
                if bool(v.get("fullscreen", True)):
                    args.append("--fullscreen")
                else:
                    args.append("--no-fullscreen")
                self._instance = self._vlc.Instance(args)
                self._player = self._instance.media_player_new()
                self._connected = True
                self._last_error = ""
                try:
                    self._version = str(self._vlc.libvlc_get_version() or "")
                except Exception:
                    self._version = ""
                # Aviso de fin de reproducción
                try:
                    em = self._player.event_manager()
                    em.event_attach(self._vlc.EventType.MediaPlayerEndReached,
                                    self._cb_end_reached)
                except Exception:
                    pass
                return True, ""
            except Exception as e:
                self._connected = False
                self._last_error = f"VLC: {e}"
                return False, self._last_error

    def _cb_end_reached(self, event):
        self._end_pending = True
        cb = self._on_end
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                if self._player is not None:
                    self._player.stop()
                    self._player.release()
            except Exception:
                pass
            try:
                if self._instance is not None:
                    self._instance.release()
            except Exception:
                pass
            self._player = None
            self._instance = None
            self._media = None
            self._connected = False
            self._expected_uri = ""

    # ------------------------------------------------------------ consultas
    def _state_name(self) -> str:
        try:
            s = self._player.get_state()
            s = int(s)
        except Exception:
            return STATE_STOPPED
        mapping = {
            0: STATE_IDLE,       # NothingSpecial
            1: STATE_OPENING,
            2: STATE_BUFFERING,
            3: STATE_PLAYING,
            4: STATE_PAUSED,
            5: STATE_STOPPED,
            6: STATE_ENDED,
            7: STATE_ERROR,
        }
        return mapping.get(s, STATE_STOPPED)

    def snapshot(self) -> PlayerSnapshot:
        snap = PlayerSnapshot()
        snap.available = self._connected
        snap.error = self._last_error
        snap.volume = self._volume
        with self._lock:
            if not self._connected or self._player is None:
                return snap
            try:
                state = self._state_name()
                snap.state = state
                if self._end_pending and state == STATE_ENDED:
                    snap.movie_ended = True
                if state in (STATE_PLAYING, STATE_PAUSED, STATE_BUFFERING):
                    snap.position_ms = max(0, int(self._player.get_time() or 0))
                    snap.length_ms = max(0, int(self._player.get_length() or 0))
                uri = self.uri_now()
                if uri:
                    snap.uri = uri
                    snap.has_input = True
                try:
                    snap.track_audio = int(self._player.audio_get_track() or -1)
                except Exception:
                    snap.track_audio = -1
                try:
                    snap.track_sub = int(self._player.video_get_spu() or -1)
                except Exception:
                    snap.track_sub = -1
            except Exception as e:
                snap.error = f"lectura de estado: {e}"
        return snap

    def has_input(self) -> bool:
        return bool(self.uri_now())

    def uri_now(self) -> str:
        with self._lock:
            if not self._connected or self._player is None:
                return ""
            try:
                m = self._player.get_media()
                if not m:
                    return self._expected_uri if False else ""
                return str(m.get_mrl() or "")
            except Exception:
                return ""

    def position_ms(self) -> int:
        try:
            t = int(self._player.get_time() or 0)
            return max(0, t)
        except Exception:
            return 0

    def length_ms(self) -> int:
        try:
            t = int(self._player.get_length() or 0)
            return max(0, t)
        except Exception:
            return 0

    # ------------------------------------------------------------ control
    def open_uri(self, uri: str, start_ms: int = 0, options: Optional[List[str]] = None,
                 volume: int = 100) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        with self._lock:
            self._end_pending = False
            try:
                if self._player is not None:
                    self._player.stop()
            except Exception:
                pass
            try:
                if self._media is not None:
                    self._media.release()
            except Exception:
                pass
            self._media = None
            try:
                media = self._vlc.Media(self._instance, str(uri))
                # python-vlc elige new_path / new_location según el formato.
                for opt in options or ():
                    try:
                        media.add_option(str(opt))
                    except Exception:
                        pass
                self._media = media
                self._player.set_media(media)
                self._expected_uri = str(uri)
                if volume and volume > 0:
                    self._volume = int(volume)
                    try:
                        self._player.audio_set_volume(self._volume)
                    except Exception:
                        pass
                self._player.play()
                self._end_pending = False
                if start_ms and start_ms > 0:
                    self._wait_ready(2500)
                    self.seek(int(start_ms))
                return {"ok": True, "uri": str(uri), "start_ms": int(start_ms or 0)}
            except Exception as e:
                self._last_error = f"abrir media: {e}"
                return {"ok": False, "error": self._last_error}

    def _wait_ready(self, timeout_ms: int = 3000) -> None:
        """Espera a que el input exista para poder consultar pistas / hacer seek."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            try:
                state = self._state_name()
                if state in (STATE_PLAYING, STATE_PAUSED, STATE_BUFFERING):
                    return
                if state in (STATE_ENDED, STATE_ERROR):
                    return
            except Exception:
                pass
            time.sleep(0.08)

    def select_tracks(self, audio_index: int = -1, subtitle_index: int = -1,
                      external_sub: Optional[str] = None) -> dict:
        """Selecciona audio/subtítulo de la media ya cargada.

        Mapea el ordinal guardado por ffprobe a la pista real de VLC.
        Si hay subtítulo externo, primero intenta engancharlo como slave
        (requiere VLC 3.x + python-vlc reciente) y como respaldo usa la
        opción :sub-file= en una recarga con opciones.
        """
        self._expected_audio = int(audio_index if audio_index is not None else -1)
        self._expected_sub = int(subtitle_index if subtitle_index is not None else -1)
        self._expected_external_sub = external_sub
        result: Dict[str, object] = {
            "ok": True, "audio_es": None, "sub_es": None, "external_sub": external_sub,
        }
        if not self._connected or self._player is None:
            return {"ok": False, "error": "VLC no conectado"}
        try:
            if external_sub and Path(str(external_sub)).exists():
                attached = self._try_add_slave(str(external_sub))
                result["slave_ok"] = bool(attached)
                if not attached:
                    # Reintento con sub-file embebido en la media.
                    reload = self._reload_with_sub_option(str(external_sub))
                    result["reload_with_option"] = reload.get("ok", False)
                    if not reload.get("ok", False):
                        result["slave_warning"] = reload.get("error") or "no se pudo adjuntar SRT"
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
            return result

        # Audio: seleccionar pista (0 = primera / por defecto no tocar a menos
        # que el motor lo pida explícitamente con índice >= 0)
        if self._expected_audio >= 0:
            try:
                aud = self.audio_tracks()
                es = _pick_es_by_ordinal(aud, self._expected_audio)
                if es is None and aud:
                    es = [t.es_id for t in aud if t.es_id >= 0][0] if any(t.es_id >= 0 for t in aud) else None
                if es is not None:
                    self._player.audio_set_track(int(es))
                    result["audio_es"] = es
            except Exception as e:
                result["audio_error"] = str(e)

        # Subtítulos
        try:
            subs = self.subtitle_tracks()
        except Exception:
            subs = []
        if self._expected_sub is None or self._expected_sub < 0:
            # Sin subtítulos: desactivar explícitamente
            try:
                self._player.video_set_spu(-1)
                result["sub_es"] = -1
            except Exception:
                pass
        else:
            es = None
            try:
                es = _pick_es_by_ordinal(subs, self._expected_sub)
            except Exception:
                es = None
            if es is None:
                # Último intento: lenguaje de la fila ffprobe
                lang = ""
                try:
                    es = _pick_es_by_language(subs, lang)
                except Exception:
                    es = None
            if es is not None:
                try:
                    self._player.video_set_spu(int(es))
                    result["sub_es"] = es
                except Exception as e:
                    result["sub_error"] = str(e)
        return result

    def _try_add_slave(self, sub_path: str) -> bool:
        try:
            method = getattr(self._player, "add_slave", None)
            if method is None:
                return False
            mst = getattr(self._vlc, "MediaSlaveType", None)
            stype = getattr(mst, "subtitle", 0)
            return int(method(int(stype), sub_path, True) or 0) == 0
        except Exception:
            return False

    def _reload_with_sub_option(self, sub_path: str) -> dict:
        """Recarga la media actual con :sub-file= como opción de entrada."""
        uri = self.uri_now()
        if not uri:
            return {"ok": False, "error": "no hay media cargada"}
        # Las opciones de libvlc se pasan sin espacios sin escapar; convertimos
        # la ruta a forward slashes y escapamos los espacios con %20.
        fwd = str(sub_path).replace("\\", "/")
        escaped = re.sub(r"([ #'\"%])", lambda m: "%" + hex(ord(m.group(1)))[2:].upper(), fwd)
        opt = f":sub-file={escaped}"
        start = self.position_ms()
        with self._lock:
            res = self.open_uri(uri, start_ms=int(start), options=[opt],
                                volume=self._volume)
            self._media = None
        return res

    def play(self) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        try:
            self._player.play()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pause(self) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        try:
            self._player.set_pause(1)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        try:
            self._player.stop()
            self._end_pending = False
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def seek(self, ms: int) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        target = max(0, int(ms or 0))
        try:
            self._wait_ready(2000)
            self._player.set_time(target)
            # verificación
            time.sleep(0.12)
            actual = self.position_ms()
            return {"ok": True, "target_ms": target, "actual_ms": actual}
        except Exception as e:
            return {"ok": False, "target_ms": target, "actual_ms": self.position_ms(),
                    "error": str(e)}

    def set_volume(self, value: int) -> dict:
        try:
            self._volume = max(0, min(200, int(value)))
            self._player.audio_set_volume(self._volume)
            return {"ok": True, "volume": self._volume}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def audio_tracks(self) -> List[TrackDesc]:
        try:
            raw = self._player.audio_get_track_description() or ()
            return [TrackDesc(getattr(t, "i_id", -1), getattr(t, "psz_name", "") or "") for t in raw]
        except Exception:
            return []

    def subtitle_tracks(self) -> List[TrackDesc]:
        try:
            raw = self._player.video_get_spu_description() or ()
            return [TrackDesc(getattr(t, "i_id", -1), getattr(t, "psz_name", "") or "") for t in raw]
        except Exception:
            return []

    def set_on_end(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_end = callback


# ---------------------------------------------------------------------------
# Constructor por configuración
# ---------------------------------------------------------------------------
def build_player(cfg: dict) -> BasePlayer:
    return VlcPlayer(cfg)
