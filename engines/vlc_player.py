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
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Localización de la APLICACIÓN VLC ya instalada (vlc.exe / VLC.app / cvlc)
# ---------------------------------------------------------------------------
VLC_EXE_CANDIDATES: List[str] = []


def find_vlc_executable(cfg: dict) -> str:
    """Ruta al ejecutable de la app VLC instalada (no a libvlc).

    Se usa para controlar el VLC del sistema por su interfaz HTTP: así la
    VENTANA de VLC es única y persistente y OBS puede capturarla por ventana
    sin que cambie de handle al pasar de una película a otra.
    """
    configured = ""
    v = (cfg or {}).get("vlc") or {}
    for key in ("exe", "path"):
        raw = str(v.get(key) or "").strip().strip('"')
        if raw:
            p = Path(raw)
            configured = str(p if p.suffix.lower() == ".exe" or p.is_file() else p /
                             ("VLC.exe" if sys.platform.startswith("win") else "vlc"))
            if Path(configured).exists():
                return configured
    dirs: List[str] = []
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
        dirs += [r"C:\VideoLAN\VLC", r"C:\VLC"]
        names = ["vlc.exe", "VLC.exe"]
        on_path = shutil.which("vlc.exe") or shutil.which("vlc")
        if on_path:
            dirs.insert(0, str(Path(on_path).parent))
    elif sys.platform == "darwin":
        dirs += ["/Applications/VLC.app/Contents/MacOS"]
        names = ["VLC", "vlc"]
    else:
        dirs += ["/usr/bin", "/usr/local/bin", "/snap/bin"]
        names = ["vlc", "cvlc"]
        on_path = shutil.which("vlc")
        if on_path:
            dirs.insert(0, str(Path(on_path).parent))
    seen: List[str] = []
    for d in dirs + ([str(Path(configured).parent)] if configured else []):
        if d and d not in seen:
            seen.append(d)
            for n in names:
                cand = Path(d) / n
                if cand.exists():
                    return str(cand)
    # Si lo configurado no existe, devolverlo igual (el motor lo reportará)
    return configured or ""

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
    """Localiza libvlc Y/O la app VLC instalada y devuelve un resumen a la UI."""
    result = {
        "ok": False,
        "error": "",
        "lib_dir": "",
        "lib_file": "",
        "module_available": VLC_BINDINGS_AVAILABLE,
        "vlc_version": "",
        "mode": str((cfg or {}).get("vlc", {}).get("mode") or "app"),
        "vlc_exe": find_vlc_executable(cfg),
    }
    if not VLC_BINDINGS_AVAILABLE and not result["vlc_exe"]:
        result["error"] = VLC_BINDINGS_ERROR or "No se encontro la app VLC instalada."
        return result
    dirs = _candidate_lib_dirs(cfg)
    found = _find_libvlc_file(dirs)
    if found:
        result["lib_dir"] = str(Path(found).parent)
        result["lib_file"] = found
    if result["lib_dir"]:
        os.environ["PYTHON_VLC_MODULE_PATH"] = result["lib_dir"]
    # Modo "app" (VLC instalado, ventana unica para OBS) solo necesita el
    # ejecutable; el modo "libvlc" necesita los bindings + la libreria.
    if result["mode"] == "libvlc":
        if not VLC_BINDINGS_AVAILABLE:
            result["error"] = VLC_BINDINGS_ERROR
            return result
    else:
        result["mode"] = "app" if result["vlc_exe"] else "libvlc"
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
                    "--no-embedded-video",
                    "--network-caching=%d" % int(v.get("network_caching", 300) or 300),
                    "--audio-language=%s" % (v.get("audio_language") or "es,en,spa"),
                    "--sub-language=%s" % (v.get("sub_language") or "es,en,spa"),
                ]
                # NOTA: pantalla completa NO se fuerza a nivel de instancia.
                # Se aplica al iniciar cada archivo (_apply_fullscreen); así
                # VLC no abre/cierra ventanas a negras con cada reintento.
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
                    return ""
                return str(m.get_mrl() or "")
            except Exception:
                return ""

    @staticmethod
    def _mrl_disk_path(mrl: str) -> str:
        """Convierte un MRL (file:///C:/...) a ruta local de disco."""
        v = str(mrl or "")
        if v.lower().startswith("file://"):
            v = v[7:]
            try:
                from urllib.parse import unquote
                v = unquote(v)
            except Exception:
                pass
            if len(v) > 2 and v[0] == "/" and v[2] == ":":
                v = v[1:]  # file:///C:/... -> C:/...
        return v

    def _apply_fullscreen(self) -> None:
        try:
            want = bool((self.cfg.get("vlc") or {}).get("fullscreen", True))
            if want and self._player is not None:
                self._player.set_fullscreen(True)
        except Exception:
            pass

    def _libvlc_error_text(self) -> str:
        try:
            raw = self._vlc.libvlc_errmsg() or b""
            if isinstance(raw, bytes):
                return raw.decode("utf-8", "replace").strip()
            return str(raw).strip()
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
                opts = list(options or ())
                # Posición de arranque: además del set_time posterior (que a
                # veces se pierde si VLC aún está abriendo), le damos a libvlc
                # la opción :start-time para que arranque directamente en el
                # punto correcto (reanudar tras tanda / cursor del Scheduler).
                if start_ms and start_ms > 0 and not any(str(o).startswith(":start-time") for o in opts):
                    opts.append(":start-time=%.3f" % (int(start_ms) / 1000.0))
                for opt in opts:
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
                self._apply_fullscreen()
                # Esperar a que arranque y detectar fallo real de libvlc
                # (archivo inexistente, códec no soportado, etc.) para no
                # entrar en un bucle de abrir/cerrar.
                state = self._wait_ready(6000)
                if state == STATE_ERROR:
                    self._last_error = "VLC: %s" % (
                        self._libvlc_error_text() or "no se pudo reproducir el archivo.")
                    return {"ok": False, "error": self._last_error, "state": state,
                            "uri": str(uri)}
                if state == STATE_ENDED and not self.length_ms():
                    self._last_error = ("VLC: el archivo no produjo contenido "
                                        "(¿ruta inválida o archivo dañado?).")
                    return {"ok": False, "error": self._last_error, "state": state,
                            "uri": str(uri)}
                if start_ms and start_ms > 0:
                    # Respaldo explícito al :start-time: si VLC arrancó en 0
                    # (archivos en red o contenedores que ignoran la opción),
                    # se posiciona ya con reproducción activa y se verifica.
                    self._seek_verified(int(max(0, start_ms)), retries=3,
                                        tolerance_ms=2500)
                # Reintento de pantalla completa una vez que la ventana existe
                self._apply_fullscreen()
                return {"ok": True, "uri": str(uri),
                        "start_ms": int(start_ms or 0), "state": self._state_name(),
                        "position_ms": self.position_ms()}
            except Exception as e:
                self._last_error = f"abrir media: {e}"
                return {"ok": False, "error": self._last_error}

    def _seek_verified(self, target_ms: int, retries: int = 3,
                       tolerance_ms: int = 2000) -> dict:
        """set_time con verificación: algunos contenedores/red ignoran el seek
        mientras están abriendo, así que se reintenta hasta que la posición
        real se acerque al objetivo (evita que una película se reanude en 0)."""
        target = max(0, int(target_ms or 0))
        last_actual = self.position_ms()
        for attempt in range(max(1, int(retries))):
            try:
                state = self._state_name()
                if state == STATE_ERROR:
                    break
                self._player.set_time(target)
                time.sleep(0.12 + 0.08 * attempt)
                last_actual = self.position_ms()
                # VLC nunca devuelve la posición exacta; aceptar cercanía.
                if abs(last_actual - target) <= max(500, int(tolerance_ms)):
                    break
                # Si el archivo es más corto que el objetivo, no insistir.
                length = self.length_ms()
                if length and target >= length > 0:
                    break
            except Exception:
                break
        return {"ok": True, "target_ms": target, "actual_ms": int(last_actual)}

    def _wait_ready(self, timeout_ms: int = 6000) -> str:
        """Espera a que la reproducción arranque; devuelve el estado final."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        last = self._state_name()
        while time.monotonic() < deadline:
            last = self._state_name()
            if last in (STATE_PLAYING, STATE_PAUSED):
                return last
            if last in (STATE_ENDED, STATE_ERROR):
                return last
            time.sleep(0.08)
        return last

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
        with self._lock:
            try:
                self._player.play()
                self._end_pending = False
                self._apply_fullscreen()
                return {"ok": True}
            except Exception as e:
                self._last_error = f"play: {e}"
                return {"ok": False, "error": self._last_error}

    def pause(self) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        with self._lock:
            try:
                self._player.set_pause(1)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        with self._lock:
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
        with self._lock:
            try:
                state = self._wait_ready(3000)
                if state == STATE_ERROR:
                    self._last_error = "VLC: %s" % (
                        self._libvlc_error_text() or "no se pudo reproducir.")
                    return {"ok": False, "target_ms": target, "actual_ms": self.position_ms(),
                            "error": self._last_error}
                res = self._seek_verified(target, retries=4, tolerance_ms=2000)
                res["ok"] = True
                return res
            except Exception as e:
                self._last_error = f"seek: {e}"
                return {"ok": False, "target_ms": target, "actual_ms": self.position_ms(),
                        "error": self._last_error}

    def set_volume(self, value: int) -> dict:
        with self._lock:
            try:
                self._volume = max(0, min(200, int(value)))
                if self._player is not None:
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
# Reproductor: APLICACION VLC instalada, controlada por su interfaz HTTP
# ---------------------------------------------------------------------------
class VlcHttpPlayer(BasePlayer):
    """Controla el VLC del sistema (vlc.exe) que el usuario ya tiene
    instalado, mediante su interfaz web (http://host:puerto/requests/...).

    A diferencia del reproductor libvlc embebido, aqui la VENTANA de VLC es
    una sola y persistente: arrancamos la app con --extraintf=http y solo le
    ordenamos reproducir/parar/buscar. La ventana NO se cierra ni se recrea al
    cambiar de pelicula, por lo que la captura por ventana de OBS nunca pierde
    el objetivo.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        v = self.cfg.get("vlc") or {}
        self._host = str(v.get("http_host") or "127.0.0.1")
        self._port = int(v.get("http_port") or 9099)
        self._password = str(v.get("http_password") or "tvplayout")
        self._exe = find_vlc_executable(self.cfg)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._connected = False
        self._last_error = ""
        self._volume = int(v.get("volume", 100) or 100)
        self._uri = ""
        self._on_end: Optional[Callable[[], None]] = None
        self._manual_stop = False
        self._expected_audio = -1
        self._expected_sub = -1
        self._expected_external_sub = None

    # -------------------------------------------------------------- util
    @property
    def _base(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _auth_header(self) -> Dict[str, str]:
        import base64
        # VLC HTTP usa Basic auth con el password como usuario vacio: ":"+pass
        token = base64.b64encode(f":{self._password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _http(self, path: str, timeout: float = 4.0, want_xml: bool = False):
        url = self._base + path
        req = urllib.request.Request(url, headers=self._auth_header())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if want_xml:
            return ET.fromstring(data.decode("utf-8", "replace"))
        return data

    def _cmd(self, command: str, **params) -> bool:
        q = urllib.parse.urlencode({"command": command, **{k: str(v) for k, v in params.items() if v is not None}})
        try:
            self._http(f"/requests/status.xml?{q}")
            return True
        except Exception as e:
            self._last_error = f"VLC HTTP {command}: {e}"
            return False

    def _status(self) -> Optional[ET.Element]:
        try:
            return self._http("/requests/status.xml", want_xml=True)
        except Exception as e:
            self._last_error = str(e)
            return None

    def _launch_vlc(self) -> Tuple[bool, str]:
        if not self._exe or not Path(self._exe).exists():
            return False, ("No se encontro la app VLC instalada. Indica la ruta de "
                           "vlc.exe en AJUSTES VLC (o instala VLC).")
        args = [
            self._exe,
            "--intf", "qt",
            "--extraintf", "http",
            "--http-host", self._host,
            "--http-port", str(self._port),
            "--http-password", self._password,
            "--no-http-ssl",
            "--no-video-title-show",
            "--no-keyboard-events",
            "--no-mouse-events",
            "--no-one-instance",
            "--network-caching=%d" % int((self.cfg.get("vlc") or {}).get("network_caching", 300) or 300),
            "--audio-language=%s" % (v.get("audio_language") or "es,en,spa"),
            "--sub-language=%s" % (v.get("sub_language") or "es,en,spa"),
            "--preferred-resolution=-1",
        ]
        if bool((self.cfg.get("vlc") or {}).get("fullscreen", True)):
            args.append("--fullscreen")
        try:
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(args, **kwargs)
        except Exception as e:
            return False, f"No se pudo iniciar la app VLC: {e}"
        return True, ""

    # -------------------------------------------------------------- vida
    def connect(self) -> Tuple[bool, str]:
        with self._lock:
            if self._status() is not None:
                self._connected = True
                self._last_error = ""
                return True, ""
            ok, err = self._launch_vlc()
            if not ok:
                self._connected = False
                self._last_error = err
                return False, err
            # Esperar a que la interfaz HTTP responda
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                if self._status() is not None:
                    self._connected = True
                    self._last_error = ""
                    try:
                        self.set_volume(self._volume)
                    except Exception:
                        pass
                    return True, ""
                time.sleep(0.4)
            self._connected = False
            self._last_error = "VLC no respondio por HTTP al arrancar."
            return False, self._last_error

    def close(self) -> None:
        # NO cerramos la ventana de VLC: es la que captura OBS. Solo soltamos.
        self._connected = False

    # --------------------------------------------------------- consultas
    @staticmethod
    def _txt(el: Optional[ET.Element], tag: str, default: str = "") -> str:
        if el is None:
            return default
        node = el.find(tag)
        return node.text if node is not None and node.text is not None else default

    def snapshot(self) -> PlayerSnapshot:
        snap = PlayerSnapshot()
        snap.volume = self._volume
        root = self._status()
        if root is None:
            snap.available = False
            snap.error = self._last_error or "VLC no responde"
            snap.state = STATE_ERROR
            return snap
        snap.available = True
        snap.error = ""
        state_map = {"playing": STATE_PLAYING, "paused": STATE_PAUSED,
                     "stopped": STATE_STOPPED, "error": STATE_ERROR,
                     "buffering": STATE_BUFFERING, "opening": STATE_OPENING,
                     "es-added": STATE_OPENING}
        raw_state = (self._txt(root, "state") or "stopped").strip()
        state = state_map.get(raw_state, STATE_STOPPED)
        snap.state = state
        try:
            snap.position_ms = max(0, int(float(self._txt(root, "time") or 0)) * 1000)
        except Exception:
            snap.position_ms = 0
        try:
            snap.length_ms = max(0, int(float(self._txt(root, "length") or 0)) * 1000)
        except Exception:
            snap.length_ms = 0
        try:
            # Volumen de VLC: 0..512 (256 = 100%)
            vv = int(float(self._txt(root, "volume") or 256))
            snap.volume = max(0, min(200, round(vv / 256.0 * 100)))
        except Exception:
            snap.volume = self._volume
        if state == STATE_STOPPED and self._uri:
            # Archivo termino (o stop manual): lo reflejamos como fin para que
            # el motor no recargue en bucle.
            snap.movie_ended = True
        snap.uri = self._uri if state != STATE_STOPPED else ""
        snap.has_input = bool(snap.uri)
        return snap

    def has_input(self) -> bool:
        snap = self.snapshot()
        return bool(snap.available and snap.state in (STATE_PLAYING, STATE_PAUSED, STATE_BUFFERING, STATE_OPENING))

    def uri_now(self) -> str:
        return self._uri

    def position_ms(self) -> int:
        return self.snapshot().position_ms

    def length_ms(self) -> int:
        return self.snapshot().length_ms

    # --------------------------------------------------------- control
    def _to_mrl(self, uri: str) -> str:
        u = str(uri)
        if re.match(r"^[a-zA-Z]+://", u):
            return u
        return Path(u).resolve().as_uri()

    def open_uri(self, uri: str, start_ms: int = 0, options: Optional[List[str]] = None,
                 volume: int = 100) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        with self._lock:
            self._manual_stop = False
            mrl = self._to_mrl(uri)
            opts: List[str] = []
            sub_file = ""
            for o in options or ():
                o = str(o)
                if o.startswith(":sub-file="):
                    sub_file = o.split("=", 1)[1]
                elif o.startswith(":start-time="):
                    pass  # start_ms ya lo gestiona el seek
                else:
                    opts.append(o)
            try:
                # Limpiar la lista y anadir el archivo (manteniendo la ventana).
                self._cmd("pl_empty")
                in_url = mrl
                extra = list(opts)
                if start_ms and start_ms > 0:
                    extra.append(f":start-time={int(start_ms)/1000.0:.3f}")
                if sub_file:
                    extra.append(f":sub-file={sub_file}")
                if extra:
                    in_url = mrl + " :option=" + " :option=".join(urllib.parse.quote(x) for x in extra)
                q = urllib.parse.urlencode({"command": "in_play", "input": in_url})
                self._http(f"/requests/status.xml?{q}")
                self._uri = str(uri)
                if volume and volume > 0:
                    self.set_volume(int(volume))
                # Esperar a que arranque / detectar error
                state = self._wait_ready(8000)
                if state == STATE_ERROR:
                    self._last_error = "VLC no pudo reproducir el archivo (¿ruta o codec?)."
                    return {"ok": False, "error": self._last_error, "state": state, "uri": str(uri)}
                if start_ms and start_ms > 0:
                    self._seek_verified(int(start_ms), retries=4, tolerance_ms=2500)
                if bool((self.cfg.get("vlc") or {}).get("fullscreen", True)):
                    self._cmd("fullscreen", val=1)
                return {"ok": True, "uri": str(uri), "start_ms": int(start_ms or 0),
                        "state": state, "position_ms": self.position_ms()}
            except Exception as e:
                self._last_error = f"abrir media: {e}"
                return {"ok": False, "error": self._last_error}

    def _wait_ready(self, timeout_ms: int = 8000) -> str:
        deadline = time.monotonic() + timeout_ms / 1000.0
        last = STATE_STOPPED
        while time.monotonic() < deadline:
            snap = self.snapshot()
            last = snap.state
            if last in (STATE_PLAYING, STATE_PAUSED):
                return last
            if last in (STATE_ENDED, STATE_ERROR):
                return last
            time.sleep(0.2)
        return last

    def _seek_verified(self, target_ms: int, retries: int = 4, tolerance_ms: int = 2000) -> dict:
        target = max(0, int(target_ms or 0))
        actual = self.position_ms()
        for attempt in range(max(1, int(retries))):
            val = target // 1000
            self._cmd("seek", val=val)
            time.sleep(0.25 + 0.1 * attempt)
            actual = self.position_ms()
            if abs(actual - target) <= max(1000, int(tolerance_ms)):
                break
            length = self.length_ms()
            if length and target >= length:
                break
        return {"ok": True, "target_ms": target, "actual_ms": int(actual)}

    def select_tracks(self, audio_index: int = -1, subtitle_index: int = -1,
                      external_sub: Optional[str] = None) -> dict:
        # El reproductor HTTP cambia pistas con atajos de teclado enviados al
        # VLC: 'a' cicla audio, 'v' cicla subtitulos. Es best-effort; la
        # seleccion fina (ordinal exacto) se aplica en open_uri con opciones.
        self._expected_audio = int(audio_index if audio_index is not None else -1)
        self._expected_sub = int(subtitle_index if subtitle_index is not None else -1)
        self._expected_external_sub = external_sub
        result = {"ok": True, "audio_es": None, "sub_es": None,
                  "external_sub": external_sub, "note": "tracks via hotkeys"}
        try:
            if external_sub and Path(str(external_sub)).exists():
                # Recargar con el SRT externo es la via fiable por HTTP.
                pos = self.position_ms()
                res = self.open_uri(self._uri, start_ms=pos,
                                    options=[f":sub-file={urllib.parse.quote(str(external_sub))}"],
                                    volume=self._volume)
                result["reloaded_with_sub"] = bool(res.get("ok"))
            # Audio: ciclar pistas con 'a' las veces que haga falta (best effort)
            if self._expected_audio > 0:
                for _ in range(int(self._expected_audio)):
                    self._cmd("pl_forceplay")  # no-op seguro
                    self._key("a")
                    time.sleep(0.05)
            # Subtitulos: VLC arranca sin subs; 'v' cicla.
            if self._expected_sub < 0:
                pass
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
        return result

    def _key(self, keyname: str) -> bool:
        # La interfaz HTTP no expone teclas directamente; key-disc es el
        # endpoint no documentado que acepta el codigo de tecla.
        keymap = {"a": 0x61, "v": 0x76}
        code = keymap.get(keyname)
        if code is None:
            return False
        try:
            q = urllib.parse.urlencode({"code": code})
            self._http(f"/requests/key-disc.xml?{q}")
            return True
        except Exception:
            return False

    def play(self) -> dict:
        return {"ok": self._cmd("pl_play") or self._cmd("play")}

    def pause(self) -> dict:
        return {"ok": self._cmd("pl_pause") or self._cmd("pause")}

    def stop(self) -> dict:
        self._manual_stop = True
        return {"ok": self._cmd("pl_stop") or self._cmd("stop")}

    def seek(self, ms: int) -> dict:
        ok, err = self.connect()
        if not ok:
            return {"ok": False, "error": err}
        return self._seek_verified(int(ms), retries=4, tolerance_ms=2000)

    def set_volume(self, value: int) -> dict:
        with self._lock:
            self._volume = max(0, min(200, int(value)))
            vv = round(self._volume / 100.0 * 256)
            return {"ok": self._cmd("volume", val=vv), "volume": self._volume}

    def audio_tracks(self) -> List[TrackDesc]:
        return []  # La interfaz HTTP no lista pistas; best-effort por idioma.

    def subtitle_tracks(self) -> List[TrackDesc]:
        return []

    def set_on_end(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_end = callback



# ---------------------------------------------------------------------------
# Constructor por configuración
# ---------------------------------------------------------------------------
def build_player(cfg: dict) -> BasePlayer:
    """Construye el reproductor segun la configuracion.

    - mode="app" (por defecto): controla la APLICACION VLC instalada por su
      interfaz HTTP. Una sola ventana persistente -> OBS la captura por
      ventana sin perderla al cambiar de pelicula.
    - mode="libvlc": ventana embebida via python-vlc (respaldo si no hay app).
    Si el modo pedido no esta disponible, cae al otro automaticamente.
    """
    v = (cfg or {}).get("vlc") or {}
    mode = str(v.get("mode") or "app").lower()
    exe = find_vlc_executable(cfg)
    if mode == "app" and exe:
        return VlcHttpPlayer(cfg)
    if mode == "app" and not exe:
        # No hay app VLC: intentar libvlc como respaldo.
        if VLC_BINDINGS_AVAILABLE:
            return VlcPlayer(cfg)
        return VlcHttpPlayer(cfg)  # reportara el error de ejecutable no hallado
    return VlcPlayer(cfg)
