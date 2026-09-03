"""
TVPlayout — Motor de playout para VLC (sin OBS).

El Scheduler (SQLite) es la fuente de verdad. El reloj manda: cada ~0,5 s el
motor mira qué evento debe estar al aire y controla el reproductor VLC real.

Semántica del evento "siguiente película":
  - Cuando el evento al aire cambia (llega la hora de la siguiente fila del
    Scheduler), el motor carga el archivo de esa fila y lo reanuda donde toca.
  - Si VLC no tiene cargado el archivo del evento actual (arranque del panel,
    VLC caído, error, película terminada), el motor lo recarga solo.

Tandas comerciales:
  - Los eventos COMMERCIAL (manuales, AUTO_ADS o AUTO_WEEKLY) interrumpen la
    película en su posición REAL de VLC; al terminar la tanda la película se
    reanuda exactamente en ese punto (o con el cursor calculado del Scheduler
    si el panel se reinició a mitad de tanda).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .vlc_player import BasePlayer, PlayerSnapshot

IDLE = "IDLE"
PROGRAM = "PROGRAM"
COMMERCIAL = "COMMERCIAL"

# Cuánto pasado se tolera antes de "barrer" tandas AUTO_ADS ya vencidas
AUTO_ADS_CLEANUP_MARGIN_MIN = 3


def _safe_json(value) -> List[dict]:
    if not value:
        return []
    try:
        data = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _now_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_display_title(value) -> str:
    """Título presentable a partir del nombre de archivo (sin año ni tags)."""
    import re
    x = Path(str(value or "")).stem.strip()
    x = re.sub(r"[._]+", " ", x)
    x = re.sub(r"\[[^\]]*\]|\([^)]*?(?:1080p|2160p|720p|WEB[- ]?DL|WEBRip|BluRay|HDR|x264|x265|HEVC|AAC|DDP|DTS)[^)]*\)", " ", x, flags=re.I)
    x = re.sub(r"\b(19|20)\d{2}\b\s*$", "", x)
    x = re.sub(r"\s+(19|20)\d{2}\s*$", "", x)
    x = re.sub(r"\s+", " ", x).strip(" -_")
    return x or str(value or "").strip()


class PlayoutEngine:
    """Motor de playout: Scheduler + VLC + tandas comerciales."""

    def __init__(self, cfg: dict, db_path, player: BasePlayer,
                 base_dir: Optional[Path] = None, clock: Optional[Callable[[], datetime]] = None):
        self.cfg = cfg or {}
        self.db_path = str(db_path)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.player = player
        self.clock = clock or datetime.now

        self._lock = threading.RLock()
        self._last_event_key: Optional[tuple] = None   # (id, start_at)
        self._take: Optional[dict] = None              # toma manual (override)
        self._interrupt: Optional[dict] = None          # película interrumpida por tanda
        self._ad_phase: bool = False
        self._last_error: str = ""
        self._last_player_connect: float = 0.0
        self._last_reload_key: Optional[tuple] = None
        self._last_reload_error: float = 0.0
        self._reload_cooldown_until: float = 0.0        # evita reintentos en bucle
        self._end_events = 0
        self.player.set_on_end(self._on_player_end)

        # Estado visible para la UI
        self.ui = {
            "mode": IDLE,
            "current": None,
            "next": None,
            "upcoming": [],
            "ad_break": False,
            "interrupted_title": None,
        }
        self._ads_engine = None
        try:
            from .ads_engine import AdsConfig, AdsEngine

            acfg = self.cfg.get("auto_ads") or {}
            self._ads_config = AdsConfig(
                enabled=bool(acfg.get("enabled")),
                interval_minutes=int(acfg.get("interval_minutes", 60) or 60),
                min_ads=int(acfg.get("min_ads", 1) or 1),
                max_ads=int(acfg.get("max_ads", 4) or 4),
                category=str(acfg.get("category") or "Commercial"),
                avoid_repeat=bool(acfg.get("avoid_repeat", True)),
                min_program_tail_seconds=int(acfg.get("min_program_tail_seconds", 90) or 90),
            )
            self._ads_engine = AdsEngine(self.db_path, self._ads_config)
        except Exception:
            self._ads_engine = None
        self._ads_lock = threading.Lock()

    # ------------------------------------------------------------------ db
    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _row(self, r) -> dict:
        return dict(r) if r is not None else None

    def _find_current(self, now: datetime) -> Optional[dict]:
        stamp = _now_stamp(now)
        c = self._db()
        try:
            r = c.execute(
                """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                   FROM schedule s JOIN media m ON m.id = s.media_id
                   WHERE s.start_at <= ? AND s.end_at > ?
                     AND s.status IN ('scheduled','playing')
                   ORDER BY s.start_at DESC, s.id DESC LIMIT 1""",
                (stamp, stamp)).fetchone()
            return self._row(r)
        finally:
            c.close()

    def _find_program_containing(self, ad_row: dict, now: datetime) -> Optional[dict]:
        """Evento PROGRAM cuya ventana contiene la posición actual (para poder
        interrumpir/reanudar la película cuando suena una tanda)."""
        stamp = _now_stamp(now)
        c = self._db()
        try:
            r = c.execute(
                """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                   FROM schedule s JOIN media m ON m.id = s.media_id
                   WHERE s.kind = 'PROGRAM'
                     AND s.start_at <= ? AND s.end_at > ?
                     AND s.start_at <= ?
                   ORDER BY s.start_at DESC, s.id DESC LIMIT 1""",
                (stamp, stamp, str(ad_row.get("start_at") or stamp))).fetchone()
            return self._row(r)
        finally:
            c.close()

    def _find_next(self, now: datetime) -> Optional[dict]:
        stamp = _now_stamp(now)
        c = self._db()
        try:
            r = c.execute(
                """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                   FROM schedule s JOIN media m ON m.id = s.media_id
                   WHERE s.status = 'scheduled' AND s.start_at > ?
                   ORDER BY s.start_at, s.id LIMIT 1""", (stamp,)).fetchone()
            return self._row(r)
        finally:
            c.close()

    def _find_upcoming(self, now: datetime, limit: int = 5) -> List[dict]:
        stamp = _now_stamp(now)
        c = self._db()
        try:
            rows = c.execute(
                """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                   FROM schedule s JOIN media m ON m.id = s.media_id
                   WHERE s.status = 'scheduled' AND s.start_at > ?
                   ORDER BY s.start_at, s.id LIMIT ?""", (stamp, int(limit))).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def _mark_playing(self, event_id) -> None:
        if not event_id:
            return
        c = self._db()
        try:
            c.execute("UPDATE schedule SET status='playing' WHERE id=?", (int(event_id),))
            c.commit()
        finally:
            c.close()

    def _mark_played(self, event_id) -> None:
        if not event_id:
            return
        c = self._db()
        try:
            c.execute("UPDATE schedule SET status='played' WHERE id=?", (int(event_id),))
            c.commit()
        finally:
            c.close()

    def _mark_played_if_finished(self, event_id, now: datetime) -> None:
        """Marca 'played' un evento que ya no es el actual y cuya ventana cerró."""
        if not event_id:
            return
        c = self._db()
        try:
            row = c.execute("SELECT end_at,status FROM schedule WHERE id=?",
                            (int(event_id),)).fetchone()
            if row and row["status"] in ("scheduled", "playing"):
                if not row["end_at"] or row["end_at"] <= _now_stamp(now):
                    c.execute("UPDATE schedule SET status='played' WHERE id=?", (int(event_id),))
                    c.commit()
        finally:
            c.close()

    def _asrun(self, row: dict) -> None:
        ai = int(row.get("audio_index") or 0)
        si = int(row.get("subtitle_index") if row.get("subtitle_index") is not None else -1)
        c = self._db()
        try:
            c.execute(
                """INSERT INTO asrun(event_time, media_id, title, kind, audio_index,
                                     subtitle_index, duration, status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 row.get("media_id", row.get("id")),
                 row.get("title") or "", row.get("kind") or PROGRAM,
                 ai, si, float(row.get("duration") or 0), "PLAYED"))
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------- helpers cursor
    def _scheduler_cursor_ms(self, row: dict, now: datetime) -> int:
        """Milisegundos transcurridos desde el inicio programado del evento."""
        try:
            start = row.get("start_at") or row.get("start")
            if not start:
                return 0
            dt = datetime.fromisoformat(str(start).replace("Z", ""))
            elapsed = max(0.0, (now - dt).total_seconds() * 1000.0)
            duration = float(row.get("duration") or 0) * 1000.0
            if duration > 0:
                elapsed = min(elapsed, max(0.0, duration - 500))
            return int(elapsed)
        except Exception:
            return 0

    def _ads_elapsed_within(self, program_row: dict, now: datetime) -> int:
        """Suma (ms) del tiempo de comerciales ya emitidos dentro de la ventana
        del programa. Se usa para reanudar la película en el punto correcto
        cuando no pudimos capturar la posición real (p. ej. tras reiniciar el
        panel a mitad de tanda)."""
        try:
            pstart = str(program_row.get("start_at") or "")
            stamp = _now_stamp(now)
            c = self._db()
            try:
                rows = c.execute(
                    """SELECT s.start_at, s.end_at, s.duration FROM schedule s
                       WHERE s.kind='COMMERCIAL' AND s.start_at >= ? AND s.start_at < ?
                         AND s.start_at < ?""",
                    (pstart, str(program_row.get("end_at") or stamp), stamp)).fetchall()
            finally:
                c.close()
            total_ms = 0.0
            for r in rows:
                end = r["end_at"]
                if end <= stamp:
                    total_ms += float(r["duration"] or 0) * 1000.0
                else:
                    try:
                        start = datetime.fromisoformat(str(r["start_at"]))
                        total_ms += max(0.0, (now - start).total_seconds() * 1000.0)
                    except Exception:
                        pass
            return int(total_ms)
        except Exception:
            return 0

    def _resume_cursor_ms(self, program_row: dict, now: datetime) -> int:
        base = self._scheduler_cursor_ms(program_row, now)
        ads = self._ads_elapsed_within(program_row, now)
        return max(0, base - ads)

    # ------------------------------------------------------- media helpers
    def _audio_sub_indexes(self, row: dict):
        try:
            ai = int(row.get("audio_index") or 0)
        except Exception:
            ai = 0
        si_raw = row.get("subtitle_index")
        try:
            si = int(si_raw) if si_raw is not None else -1
        except Exception:
            si = -1
        return ai, si

    def _external_sub_path(self, row: dict, base_dir: Optional[Path] = None) -> Optional[str]:
        """Devuelve la ruta (cacheada en UTF-8) del SRT externo seleccionado."""
        ai, si = self._audio_sub_indexes(row)
        if si < 0:
            return None
        subs = _safe_json(row.get("subs_json"))
        if si >= len(subs):
            return None
        item = dict(subs[si] or {})
        src = str(item.get("path") or "")
        if not src or not Path(src).exists():
            return None
        # Copia UTF-8 en cache; nunca toca el archivo original.
        try:
            base = Path(base_dir) if base_dir else Path.cwd()
            cache = base / "cache" / "subtitles"
            cache.mkdir(parents=True, exist_ok=True)
            p = Path(src)
            raw = p.read_bytes()
            text = None
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("latin-1", "replace")
            key = hashlib.sha1(
                (str(p.resolve()) + "|" + str(p.stat().st_mtime_ns) + "|" + str(p.stat().st_size))
                .encode("utf-8", "ignore")).hexdigest()[:16]
            out = cache / f"{p.stem}.{key}.utf8{p.suffix.lower()}"
            if not out.exists():
                out.write_text(text, encoding="utf-8", newline="\n")
            return str(out)
        except Exception:
            return src

    def _player_ready(self) -> bool:
        now = time.monotonic()
        if not self.player.has_input():
            if now - self._last_player_connect > 3.0:
                self._last_player_connect = now
                try:
                    ok, _err = self.player.connect()
                    if not ok:
                        return False
                except Exception:
                    return False
        return True

    def _load_row(self, row: dict, cursor_ms: int = 0) -> dict:
        """Carga el archivo de un evento en VLC y ajusta pistas + posición."""
        path = str(row.get("path") or "")
        if not path:
            raise RuntimeError("El evento no tiene ruta de archivo.")
        p = Path(path)
        if not p.exists():
            raise RuntimeError(f"Archivo no encontrado: {path}")
        ai, si = self._audio_sub_indexes(row)
        ext_sub = self._external_sub_path(row, self.base_dir)
        vol = int((self.cfg.get("vlc") or {}).get("volume", 100) or 100)
        res = self.player.open_uri(str(path), start_ms=int(max(0, cursor_ms or 0)),
                                   volume=vol)
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "VLC no pudo reproducir el archivo."))
        # Selección de pistas (best effort; no rompe la reproducción si falla)
        try:
            self.player.select_tracks(ai, si, ext_sub)
        except Exception:
            pass
        if cursor_ms and cursor_ms > 0:
            try:
                self.player.seek(int(cursor_ms))
            except Exception:
                pass
        return {"ok": True, "path": path, "cursor_ms": int(cursor_ms or 0)}

    # ------------------------------------------------------- transiciones
    def _event_key(self, row: dict):
        return (int(row.get("id") or 0), str(row.get("start_at") or ""))

    def _ended_but_window_open(self, row: dict, now: datetime) -> bool:
        """El archivo llegó a su fin pero la ventana del Scheduler sigue abierta
        (película/anuncio más corto de lo programado). Esperamos al siguiente
        evento en vez de recargar en bucle un archivo que termina al instante."""
        try:
            snap = self.player.snapshot()
        except Exception:
            return False
        if not (snap.movie_ended or snap.state in ("ended", "error")):
            return False
        try:
            end = datetime.fromisoformat(str(row.get("end_at") or ""))
            return (end - now).total_seconds() > 3.0
        except Exception:
            return False

    def _reload_throttled(self, row: dict, now: datetime,
                          cursor_ms: Optional[int] = None) -> dict:
        """Recarga el evento actual con enfriamiento entre intentos fallidos."""
        nowm = time.monotonic()
        if nowm < self._reload_cooldown_until:
            return {"ok": False, "skipped": "cooldown"}
        try:
            if cursor_ms is None:
                cursor_ms = self._resume_cursor_ms(row, now)
            res = self._load_row(row, int(cursor_ms))
            self._reload_cooldown_until = 0.0
            self._last_reload_error = 0.0
            return res
        except Exception as e:
            self._last_error = str(e)
            self._last_reload_error = nowm
            self._reload_cooldown_until = nowm + 2.5
            return {"ok": False, "error": str(e)}

    def _on_player_end(self) -> None:
        self._end_events += 1

    def _start_program(self, row: dict, now: datetime, cursor_ms: Optional[int] = None,
                       force: bool = False) -> dict:
        """Pone una película al aire. cursor_ms=None usa el cursor del Scheduler."""
        if cursor_ms is None:
            cursor_ms = self._scheduler_cursor_ms(row, now)
        key = self._event_key(row)
        # Evita recargar si ya está sonando este archivo en esta posición.
        if not force and key == self._last_event_key and not self._needs_reload(row):
            return {"ok": True, "reloaded": False}
        self._load_row(row, cursor_ms)
        self._last_event_key = key
        self._last_reload_key = None
        kind = str(row.get("kind") or PROGRAM)
        self.ui.update({
            "mode": kind,
            "current": {
                "schedule_id": row.get("id"),
                "media_id": row.get("media_id", row.get("id")),
                "title": row.get("title") or "",
                "duration": float(row.get("duration") or 0),
                "audio_index": int(row.get("audio_index") or 0),
                "subtitle_index": int(row.get("subtitle_index") if row.get("subtitle_index") is not None else -1),
                "kind": kind,
                "position_ms": int(cursor_ms or 0),
            },
            "ad_break": False,
        })
        self._mark_playing(row.get("id"))
        self._asrun(row)
        return {"ok": True, "reloaded": True, "cursor_ms": int(cursor_ms or 0),
                "title": row.get("title") or ""}

    def _needs_reload(self, row: dict) -> bool:
        """¿VLC está reproduciendo el archivo correcto del evento actual?"""
        try:
            snap = self.player.snapshot()
        except Exception:
            return True
        if snap.error and not snap.available:
            return True
        if snap.movie_ended or snap.state in ("ended", "error", "idle"):
            return True
        uri = snap.uri or ""
        if not uri:
            return True
        # La URI puede venir como file:///... o ruta plana
        expected = str(row.get("path") or "")
        if not expected:
            return False
        exp_norm = Path(expected).resolve()
        if uri.startswith("file://"):
            return exp_norm.as_uri() != uri.split("?")[0]
        return str(Path(uri).resolve()) != str(exp_norm)

    def _start_ad(self, ad_row: dict, now: datetime, interrupted: Optional[dict]) -> dict:
        """Pone al aire un anuncio (evento COMMERCIAL)."""
        cursor = self._scheduler_cursor_ms(ad_row, now)
        self._load_row(ad_row, cursor)
        self._last_event_key = self._event_key(ad_row)
        self.ui.update({
            "mode": COMMERCIAL,
            "current": {
                "schedule_id": ad_row.get("id"),
                "media_id": ad_row.get("media_id", ad_row.get("id")),
                "title": ad_row.get("title") or "",
                "duration": float(ad_row.get("duration") or 0),
                "audio_index": int(ad_row.get("audio_index") or 0),
                "subtitle_index": int(ad_row.get("subtitle_index") if ad_row.get("subtitle_index") is not None else -1),
                "kind": COMMERCIAL,
                "position_ms": int(cursor),
            },
            "ad_break": True,
        })
        self._mark_playing(ad_row.get("id"))
        self._asrun(ad_row)
        return {"ok": True, "ad": True, "title": ad_row.get("title") or ""}

    def _capture_interrupt(self, program_row: dict, now: datetime) -> None:
        """Guarda la posición real de la película antes de la tanda."""
        if self._interrupt and self._interrupt.get("schedule_id") == program_row.get("id"):
            return
        pos = 0
        try:
            if self.player.has_input():
                uri = self.player.uri_now() or ""
                expected = str(program_row.get("path") or "")
                norm = lambda s: str(Path(s).resolve()) if s and not s.startswith("file://") else s
                if norm(uri) == norm(expected) or (uri.startswith("file://") and Path(expected).resolve().as_uri() == uri):
                    pos = self.player.position_ms()
        except Exception:
            pos = 0
        if not pos or pos <= 0:
            pos = self._resume_cursor_ms(program_row, now)
        ai, si = self._audio_sub_indexes(program_row)
        self._interrupt = {
            "schedule_id": program_row.get("id"),
            "media_id": program_row.get("media_id", program_row.get("id")),
            "path": str(program_row.get("path") or ""),
            "title": program_row.get("title") or "",
            "audio_index": ai,
            "subtitle_index": si,
            "position_ms": int(pos),
            "captured_at": now.isoformat(timespec="seconds"),
        }
        self.ui["interrupted_title"] = self._interrupt["title"]
        self._ad_phase = True

    def _clear_interrupt(self) -> None:
        self._interrupt = None
        self._ad_phase = False
        self.ui["interrupted_title"] = None
        self.ui["ad_break"] = False

    # ------------------------------------------------------------------ tick
    def tick(self) -> dict:
        with self._lock:
            try:
                return self._tick_inner()
            except Exception as e:
                self._last_error = f"motor: {e}"
                return {"ok": False, "error": str(e)}

    def _tick_inner(self) -> dict:
        now = self.clock()
        cur = self._find_current(now)
        nxt = self._find_next(now)
        upcoming = self._find_upcoming(now, 5)

        self.ui["next"] = self._mini(nxt)
        self.ui["upcoming"] = [self._mini(r) for r in upcoming]

        if cur is not None:
            key = self._event_key(cur)
            if key != self._last_event_key and self._last_event_key is not None:
                # El evento anterior dejó de ser el actual: si su ventana ya
                # terminó, pasa a 'played'. Las tandas (ventana aún abierta)
                # NO marcan 'played' la película para poder reanudarla.
                self._mark_played_if_finished(self._last_event_key[0], now)

        if self._take is not None and now < datetime.fromisoformat(str(self._take.get("until") or "")):
            return self._tick_take(now)

        if cur is None:
            # Sin programación: dejar VLC en reposo
            if self.ui.get("mode") != IDLE or self._last_event_key is not None:
                try:
                    if self.player.has_input():
                        self.player.stop()
                except Exception:
                    pass
                self._last_event_key = None
                self._last_reload_key = None
                self._clear_interrupt()
                self.ui["mode"] = IDLE
                self.ui["current"] = None
            return {"ok": True, "event": None}

        kind = str(cur.get("kind") or PROGRAM)
        key = self._event_key(cur)
        changed = key != self._last_event_key

        if kind == COMMERCIAL:
            program = self._find_program_containing(cur, now)
            if program is None:
                # Tanda sin película debajo (hueco real de programación)
                self._clear_interrupt()
                self._ad_phase = True
            elif changed or not self._interrupt:
                self._capture_interrupt(program, now)
            if changed:
                self._start_ad(cur, now, self._interrupt)
            else:
                # Mismo anuncio: vigilar que VLC siga con él
                if not self._ended_but_window_open(cur, now):
                    try:
                        if self._needs_reload(cur):
                            cursor = self._scheduler_cursor_ms(cur, now)
                            self._reload_throttled(cur, now, cursor)
                    except Exception as e:
                        self._last_error = str(e)
            self.ui["ad_break"] = True
            return {"ok": True, "event": cur.get("id"), "kind": COMMERCIAL}

        # ---- PROGRAM ----
        if changed:
            cursor = None
            if self._interrupt and self._interrupt.get("schedule_id") == cur.get("id"):
                # Reanudar la película justo donde se cortó
                cursor = int(self._interrupt.get("position_ms") or 0)
                self._clear_interrupt()
            elif self.ui.get("mode") == COMMERCIAL or self._ad_phase:
                # Volvemos de tanda pero sin posición capturada (reinicio)
                cursor = self._resume_cursor_ms(cur, now)
                self._clear_interrupt()
            try:
                self._start_program(cur, now, cursor_ms=cursor)
            except Exception as e:
                self._last_error = f"siguiente evento: {e}"
                self.ui["mode"] = PROGRAM
                self.ui["current"] = {
                    "schedule_id": cur.get("id"),
                    "media_id": cur.get("media_id", cur.get("id")),
                    "title": cur.get("title") or "",
                    "duration": float(cur.get("duration") or 0),
                    "kind": PROGRAM,
                    "position_ms": 0,
                    "error": str(e),
                }
            return {"ok": True, "event": cur.get("id"), "kind": PROGRAM}

        # Mismo evento PROGRAM ya manejado -> salud de VLC
        if not self._ended_but_window_open(cur, now):
            try:
                if self._needs_reload(cur) and self.ui.get("mode") != COMMERCIAL:
                    cursor = self._resume_cursor_ms(cur, now)
                    res = self._reload_throttled(cur, now, cursor)
                    if res.get("ok"):
                        self._last_reload_key = key
            except Exception as e:
                self._last_error = str(e)
        return {"ok": True, "event": cur.get("id"), "kind": PROGRAM}

    def _tick_take(self, now: datetime) -> dict:
        """Mantiene la toma manual al aire hasta el próximo evento programado."""
        t = self._take or {}
        self.ui.update({
            "mode": "TAKE",
            "current": {
                "schedule_id": None,
                "media_id": t.get("media_id"),
                "title": t.get("title") or "",
                "duration": float(t.get("duration") or 0),
                "audio_index": int(t.get("audio_index") or 0),
                "subtitle_index": int(t.get("subtitle_index") if t.get("subtitle_index") is not None else -1),
                "kind": "TAKE",
                "position_ms": 0,
            },
            "ad_break": False,
            "interrupted_title": None,
        })
        # Si VLC quedó vacío durante la toma, volver a cargar el archivo.
        if not self._ended_but_window_open(t, now):
            try:
                if self._needs_reload(t):
                    self._reload_throttled(t, now, 0)
            except Exception as e:
                self._last_error = str(e)
        return {"ok": True, "event": None, "kind": "TAKE"}

    def _cancel_take(self) -> None:
        self._take = None

    def _mini(self, row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        return {
            "id": row.get("id"),
            "title": row.get("title") or "",
            "start_at": row.get("start_at"),
            "end_at": row.get("end_at"),
            "duration": float(row.get("duration") or 0),
            "kind": row.get("kind") or PROGRAM,
            "path": row.get("path") or "",
        }

    # ------------------------------------------------------------------ API
    def snapshot(self) -> dict:
        """Estado consolidado para el panel."""
        try:
            snap = self.player.snapshot()
            snapd = snap.as_dict() if hasattr(snap, "as_dict") else dict(snap)
        except Exception:
            snapd = {"available": False, "state": "error", "error": "VLC no disponible",
                     "position_ms": 0, "length_ms": 0, "playing": False}
        out = {
            "mode": self.ui.get("mode"),
            "current": self.ui.get("current"),
            "next": self.ui.get("next"),
            "upcoming": self.ui.get("upcoming"),
            "ad_break": bool(self.ui.get("ad_break")),
            "interrupted_title": self.ui.get("interrupted_title"),
            "player": snapd,
            "last_error": self._last_error,
        }
        # Posición real desde VLC cuando coincide con la película en curso
        try:
            cur = out.get("current")
            if cur and snapd.get("uri") and snapd.get("state") in ("playing", "paused"):
                cur["position_ms"] = int(snapd.get("position_ms") or cur.get("position_ms") or 0)
                cur["player_state"] = snapd.get("state")
        except Exception:
            pass
        return out

    # ------------------------------------------------ acciones del operador
    def action_play_pause(self) -> dict:
        try:
            if not self.player.has_input():
                cur = self._find_current(self.clock())
                if cur and str(cur.get("kind") or PROGRAM) != COMMERCIAL:
                    self._start_program(cur, self.clock(), force=True)
                    return {"ok": True, "action": "play"}
                return {"ok": False, "error": "No hay evento al aire."}
            st = self.player.snapshot().state
            if st == "playing":
                return self.player.pause()
            return self.player.play()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def action_stop(self) -> dict:
        return self.player.stop()

    def action_restart(self) -> dict:
        if self._take:
            t = dict(self._take)
            try:
                self._load_row(t, 0)
                cur = self.ui.get("current") or {}
                cur["position_ms"] = 0
                self.ui["current"] = cur
                return {"ok": True, "title": t.get("title") or ""}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        cur = self._find_current(self.clock())
        if not cur:
            return {"ok": False, "error": "No hay evento al aire."}
        try:
            if str(cur.get("kind") or PROGRAM) == COMMERCIAL:
                return self._start_ad(cur, self.clock(), self._interrupt)
            return self._start_program(cur, self.clock(), cursor_ms=0, force=True)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def action_next(self) -> dict:
        """Salta al siguiente evento programado (botón SIGUIENTE)."""
        now = self.clock()
        self._cancel_take()
        cur = self._find_current(now)
        if cur:
            self._mark_played(cur.get("id"))
        nxt = self._find_next(now)
        if not nxt:
            return {"ok": False, "error": "No hay siguiente evento programado"}
        try:
            self._clear_interrupt()
            if str(nxt.get("kind") or PROGRAM) == COMMERCIAL:
                program = self._find_program_containing(nxt, now)
                if program:
                    self._capture_interrupt(program, now)
                self._start_ad(nxt, now, self._interrupt)
            else:
                self._start_program(nxt, now, cursor_ms=0)
            return {"ok": True, "scheduler_id": nxt.get("id"), "title": nxt.get("title"),
                    "kind": nxt.get("kind")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def action_previous(self) -> dict:
        now = self.clock()
        self._cancel_take()
        cur = self._find_current(now)
        c = self._db()
        try:
            if cur:
                prev = c.execute(
                    """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                       FROM schedule s JOIN media m ON m.id=s.media_id
                       WHERE s.start_at < ? AND s.status IN ('scheduled','played')
                       ORDER BY s.start_at DESC LIMIT 1""",
                    (str(cur.get("start_at") or _now_stamp(now)),)).fetchone()
            else:
                prev = c.execute(
                    """SELECT s.*, m.title, m.duration, m.path, m.audio_json, m.subs_json
                       FROM schedule s JOIN media m ON m.id=s.media_id
                       WHERE s.start_at < ?
                       ORDER BY s.start_at DESC LIMIT 1""",
                    (_now_stamp(now),)).fetchone()
        finally:
            c.close()
        if not prev:
            return {"ok": False, "error": "No hay evento anterior"}
        try:
            self._clear_interrupt()
            self._start_program(dict(prev), now, cursor_ms=0)
            return {"ok": True, "scheduler_id": prev["id"], "title": prev["title"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def play_scheduled_row(self, row: dict, resume_ms: int = 0) -> dict:
        """Fuerza la reproducción de una fila (CARGAR ACTUAL / arranque)."""
        now = self.clock()
        self._cancel_take()
        row = dict(row)
        kind = str(row.get("kind") or PROGRAM)
        if resume_ms and resume_ms > 0:
            cursor = int(resume_ms)
        elif kind == COMMERCIAL:
            cursor = self._scheduler_cursor_ms(row, now)
        else:
            cursor = self._scheduler_cursor_ms(row, now)
        try:
            if kind == COMMERCIAL:
                program = self._find_program_containing(row, now)
                if program and program.get("id") != self._interrupt_id():
                    self._capture_interrupt(program, now)
                self._start_ad(row, now, self._interrupt)
            else:
                self._start_program(row, now, cursor_ms=cursor)
            return {"ok": True, "title": row.get("title") or "", "kind": kind}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _interrupt_id(self) -> Optional[int]:
        return self._interrupt.get("schedule_id") if self._interrupt else None

    def take(self, media_row: dict, audio_index: int = 0, subtitle_index: int = -1) -> dict:
        """TOMAR: reproduce una película del Media Pool al instante, sin
        alterar la programación. El siguiente tick del Scheduler respetará la
        toma hasta que toque el siguiente evento programado."""
        m = dict(media_row)
        row = {
            "id": m.get("id"),
            "media_id": m.get("id"),
            "schedule_id": None,
            "path": m.get("path"),
            "title": m.get("title") or _clean_display_title(Path(str(m.get("path") or "")).stem),
            "duration": float(m.get("duration") or 0),
            "audio_json": m.get("audio_json") or "[]",
            "subs_json": m.get("subs_json") or "[]",
            "audio_index": int(audio_index or 0),
            "subtitle_index": int(subtitle_index if subtitle_index is not None else -1),
            "kind": PROGRAM,
            "start_at": "",
        }
        try:
            self._clear_interrupt()
            self._load_row(row, 0)
            # La toma manda hasta que arranque el siguiente evento programado.
            nxt = self._find_next(self.clock())
            try:
                if nxt and nxt.get("start_at"):
                    until = datetime.fromisoformat(str(nxt["start_at"]))
                else:
                    until = self.clock() + timedelta(
                        seconds=max(float(row.get("duration") or 0), 60.0))
            except Exception:
                until = self.clock() + timedelta(seconds=300)
            self._take = {
                "media_id": row["media_id"],
                "path": row["path"],
                "title": row["title"],
                "duration": row["duration"],
                "audio_index": row["audio_index"],
                "subtitle_index": row["subtitle_index"],
                "start_at": _now_stamp(self.clock()),
                "end_at": _now_stamp(until),
                "until": until.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.ui.update({
                "mode": "TAKE",
                "current": {
                    "schedule_id": None,
                    "media_id": m.get("id"),
                    "title": row["title"],
                    "duration": row["duration"],
                    "audio_index": row["audio_index"],
                    "subtitle_index": row["subtitle_index"],
                    "kind": "TAKE",
                    "position_ms": 0,
                },
                "ad_break": False,
            })
            self._last_event_key = None  # el Scheduler no debe pelear con la toma
            self._last_reload_key = ("take", m.get("id"))
            return {"ok": True, "title": row["title"]}
        except Exception as e:
            self._take = None
            return {"ok": False, "error": str(e)}

    def reload_tracks(self, row: dict, audio_index: int, subtitle_index: int) -> dict:
        """Cambio de audio/subtítulo en vivo del evento actual."""
        row = dict(row)
        path = str(row.get("path") or "")
        if not path or not Path(path).exists():
            return {"ok": False, "error": "No existe el archivo de este evento."}
        row["audio_index"] = int(audio_index)
        row["subtitle_index"] = int(subtitle_index)
        try:
            if self.ui.get("mode") == COMMERCIAL:
                pos = self._scheduler_cursor_ms(row, self.clock())
            else:
                pos = self.player.position_ms() or 0
            ext = self._external_sub_path(row, self.base_dir)
            self.player.select_tracks(int(audio_index), int(subtitle_index), ext)
            cur = self.ui.get("current") or {}
            cur["audio_index"] = int(audio_index)
            cur["subtitle_index"] = int(subtitle_index)
            cur["position_ms"] = int(pos)
            self.ui["current"] = cur
            return {"ok": True, "cursor": int(pos), "reloaded": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def seek_to_scheduler(self) -> dict:
        """Sincroniza VLC con el cursor del Scheduler (botón SINCRONIZAR)."""
        now = self.clock()
        cur = self._find_current(now)
        if not cur:
            return {"ok": False, "error": "No hay evento al aire"}
        try:
            target = self._scheduler_cursor_ms(cur, now)
            if str(cur.get("kind") or PROGRAM) == COMMERCIAL:
                target = target % max(1, int(float(cur.get("duration") or 1) * 1000))
            res = self.player.seek(int(target))
            self.ui["current"] = {
                **(self.ui.get("current") or {}), "position_ms": int(target)
            }
            return {"ok": True, "title": cur.get("title"), "target_ms": int(target),
                    "actual_ms": int(res.get("actual_ms", 0) or 0),
                    "cursor_seconds": round(int(target) / 1000.0, 2)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def sync_status(self) -> dict:
        now = self.clock()
        cur = self._find_current(now)
        if not cur:
            return {"ok": False, "error": "No hay evento al aire"}
        snap = self.player.snapshot()
        return {
            "ok": True,
            "title": cur.get("title"),
            "target_ms": self._scheduler_cursor_ms(cur, now),
            "actual_ms": int(snap.position_ms or 0),
            "duration_ms": int(snap.length_ms or 0),
            "state": snap.state,
        }

    def player_status(self) -> dict:
        snap = self.player.snapshot()
        d = snap.as_dict()
        d["ad_break"] = bool(self.ui.get("ad_break"))
        d["mode"] = self.ui.get("mode")
        d["ready"] = bool(d.get("available") and d.get("has_input", False))
        d["source"] = "VLC"
        return d

    # ------------------------------------------------------------- tandas
    def _ads_library_pool(self) -> List[dict]:
        if self._ads_engine is None:
            return []
        try:
            return self._ads_engine.library() or []
        except Exception:
            return []

    def ad_cut_now(self) -> dict:
        """Inserta una tanda comercial inmediata (botón CORTE COMERCIAL)."""
        if self._ads_engine is None:
            return {"ok": False, "error": "Motor de anuncios no disponible."}
        if bool(self.ui.get("ad_break")):
            return {"ok": False, "error": "Ya hay una tanda en el aire."}
        now = self.clock()
        cur = self._find_current(now)
        if not cur or str(cur.get("kind") or PROGRAM) == COMMERCIAL:
            return {"ok": False, "error": "Solo se puede cortar durante una película."}
        # Una toma manual también puede cortarse: la tanda entra igual y, al
        # terminar, el Scheduler retoma el evento programado.
        self._cancel_take()
        program = cur
        pool = self._ads_library_pool()
        if not pool:
            return {"ok": False, "error": "No hay comerciales habilitados en Media Pool."}
        cfg = self.cfg.get("auto_ads") or {}
        try:
            count = random.randint(max(1, int(cfg.get("min_ads", 1) or 1)),
                                   max(int(cfg.get("max_ads", 4) or 4),
                                       int(cfg.get("min_ads", 1) or 1)))
        except Exception:
            count = 2
        try:
            pool = [dict(x) for x in pool if float(x.get("duration") or 0) > 0]
            random.shuffle(pool)
            ads = pool[:count] or pool[-count:]
        except Exception:
            ads = pool[:2]
        # Reserva el hueco: borra AUTO_ADS muy cercanos en el tiempo
        margin = 15 * 60
        c = self._db()
        try:
            c.execute(
                """DELETE FROM schedule
                   WHERE source='AUTO_ADS' AND status='scheduled'
                     AND start_at > ? AND start_at <= ?""",
                (_now_stamp(now - timedelta(seconds=30)),
                 _now_stamp(now + timedelta(seconds=margin))))
            cursor = now
            for ad in ads:
                en = cursor + timedelta(seconds=float(ad.get("duration") or 0))
                c.execute(
                    """INSERT INTO schedule(media_id,start_at,end_at,audio_index,subtitle_index,
                                            kind,status,source,day_key,generated_run)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (int(ad.get("id")), _now_stamp(cursor), _now_stamp(en), 0, -1,
                     COMMERCIAL, "scheduled", "AUTO_ADS", cursor.strftime("%Y-%m-%d"), "CUT_NOW"))
                cursor = en
            c.commit()
        finally:
            c.close()
        return {"ok": True, "ads": len(ads),
                "seconds": round(sum(float(a.get("duration") or 0) for a in ads), 1)}

    def ad_skip(self) -> dict:
        """Corta la tanda actual y vuelve a la película."""
        now = self.clock()
        cur = self._find_current(now)
        if not cur or str(cur.get("kind") or PROGRAM) != COMMERCIAL:
            return {"ok": False, "error": "No hay tanda en el aire."}
        program = self._find_program_containing(cur, now)
        c = self._db()
        try:
            # Marca jugada la tanda actual y salta las siguientes AUTO_ADS
            # muy próximas (misma tanda), para que el motor reanude la película.
            for row in c.execute(
                    """SELECT id FROM schedule
                       WHERE source='AUTO_ADS' AND status='scheduled'
                         AND start_at >= ? AND start_at <= ?""",
                    (_now_stamp(now - timedelta(seconds=30)),
                     _now_stamp(now + timedelta(seconds=1800)))).fetchall():
                c.execute("UPDATE schedule SET status='played' WHERE id=?", (row["id"],))
            c.commit()
        finally:
            c.close()
        self.ui["ad_break"] = False
        self.ui["mode"] = PROGRAM
        # Fuerza la transición: el motor reanudará la película en el próximo tick
        self._last_event_key = None
        self.ui["current"] = self._mini(program)
        return {"ok": True, "title": program["title"] if program else ""}

    def maintain_auto_ads(self) -> dict:
        """Mantiene insertadas las tandas AUTO_ADS de las próximas horas."""
        acfg = self.cfg.get("auto_ads") or {}
        if not bool(acfg.get("enabled", False)) or self._ads_engine is None:
            return {"ok": True, "enabled": False}
        with self._ads_lock:
            now = self.clock()
            horizon = now + timedelta(hours=6)
            c = self._db()
            try:
                programs = [dict(r) for r in c.execute(
                    """SELECT s.*, m.id AS mid
                       FROM schedule s JOIN media m ON m.id = s.media_id
                       WHERE s.kind='PROGRAM'
                         AND s.start_at <= ? AND s.end_at > ?
                       ORDER BY s.start_at""",
                    (_now_stamp(horizon), _now_stamp(now - timedelta(seconds=5))))]
            finally:
                c.close()
            inserted = 0
            for prog in programs[:40]:
                try:
                    preview = self._ads_engine.preview(prog["start_at"], prog["end_at"])
                    if preview.get("ok"):
                        res = self._ads_engine.insert_preview(preview)
                        inserted += int(res.get("inserted", 0) or 0)
                except Exception:
                    continue
            # Limpieza de tandas vencidas que nunca se emitieron
            try:
                c = self._db()
                c.execute(
                    """DELETE FROM schedule
                       WHERE source='AUTO_ADS' AND status='scheduled'
                         AND end_at < ?""",
                    (_now_stamp(now - timedelta(minutes=AUTO_ADS_CLEANUP_MARGIN_MIN)),))
                c.commit()
                c.close()
            except Exception:
                pass
            return {"ok": True, "inserted": inserted, "programs": len(programs)}

    # ----------------------------------------------------------------- loop
    async def run_forever(self, sleep_seconds: float = 0.5) -> None:
        last_ads_check = 0.0
        while True:
            try:
                self.tick()
            except Exception as e:
                self._last_error = str(e)
            now = time.monotonic()
            if now - last_ads_check > 45:
                last_ads_check = now
                try:
                    self.maintain_auto_ads()
                except Exception:
                    pass
            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break
