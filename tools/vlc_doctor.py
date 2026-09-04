#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostico de la conexion entre TVPlayout y la app VLC instalada.

No necesita dependencias externas (solo stdlib) ni que el panel este corriendo.
Comprueba, en este orden:

  1. Que config.json tenga el modo "app" y sus parametros HTTP.
  2. Que el ejecutable de VLC exista (misma deteccion que usa el panel).
  3. Que la interfaz HTTP de VLC responda en host:puerto con la password
     configurada (lee /requests/status.xml).
  4. Opcional: que el panel (127.0.0.1:8088) vea a VLC listo (--panel).

Uso:
    python tools/vlc_doctor.py
    python tools/vlc_doctor.py --watch           # repite cada 3 s
    python tools/vlc_doctor.py --panel           # tambien consulta al panel
    python tools/vlc_doctor.py --config C:\\TVPlayoutVLC\\config.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK = "OK"
BAD = "FALLO"
WARN = "AVISO"


def _mark(ok: bool, warn: bool = False) -> str:
    return OK if ok else (WARN if warn else BAD)


def load_config(path: str | None) -> dict:
    p = Path(path) if path else ROOT / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover - diagnostico
        print(f"[{BAD}] No pude leer {p}: {e}")
        return {}


def find_exe(cfg: dict):
    """Misma deteccion que usa el panel (engines.vlc_player)."""
    try:
        from engines.vlc_player import find_vlc_executable  # noqa: E402

        return find_vlc_executable(cfg), "engines.vlc_player"
    except Exception as e:  # pragma: no cover
        return "", f"sin modulos del panel ({e})"


def check_config(cfg: dict) -> None:
    v = cfg.get("vlc") or {}
    mode = str(v.get("mode") or "app").lower()
    host = str(v.get("http_host") or "127.0.0.1")
    port = int(v.get("http_port") or 9099)
    pwd = str(v.get("http_password") or "tvplayout")
    print("— Configuración —")
    print(f"  modo reproductor : {mode}")
    print(f"  control HTTP     : http://{host}:{port}")
    print(f"  password HTTP    : {'*' * max(1, len(pwd))} ({len(pwd)} caracteres)")
    print(f"  pantalla completa: {bool(v.get('fullscreen', True))}")
    print(f"  cache de red     : {v.get('network_caching', 300)} ms")
    if mode != "app":
        print(f"  [{WARN}] El modo configurado es '{mode}': el panel usara la ventana "
              f"embebida de libvlc, no la app VLC instalada.")


def check_exe(cfg: dict) -> str:
    exe, source = find_exe(cfg)
    print("— Ejecutable de VLC —")
    if exe and Path(exe).exists():
        print(f"  [{OK}] {exe}")
        print(f"        (detectado por {source})")
    else:
        print(f"  [{BAD}] No se encontro vlc.exe"
              + (f" (probado: {exe})" if exe else ""))
        print("        Solucion: indica la ruta completa en AJUSTES VLC -> "
              "'Ruta a vlc.exe (app)'")
    return exe


def check_http(cfg: dict, timeout: float = 3.0) -> bool:
    v = cfg.get("vlc") or {}
    host = str(v.get("http_host") or "127.0.0.1")
    port = int(v.get("http_port") or 9099)
    pwd = str(v.get("http_password") or "tvplayout")
    url = f"http://{host}:{port}/requests/status.xml"
    print("— Interfaz HTTP de VLC —")
    print(f"  consultando {url} ...")

    def _get(path_url: str, with_auth: bool):
        req = urllib.request.Request(path_url)
        if with_auth:
            token = base64.b64encode(f":{pwd}".encode("utf-8")).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    try:
        raw = _get(url, True)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  [{BAD}] VLC responde pero rechaza la password (401).")
            print("        Solucion: en AJUSTES VLC usa la misma password con la que "
                  "arranco VLC, o cierra VLC y pulsa 'INICIAR VLC' en el panel.")
        else:
            print(f"  [{BAD}] HTTP {e.code} desde VLC: {e}")
        return False
    except urllib.error.URLError as e:
        print(f"  [{BAD}] VLC no responde en {host}:{port} ({e.reason}).")
        print("        Solucion: pulsa 'INICIAR VLC' en el panel, o cierra el VLC que "
              "abriste a mano (sin --extraintf=http) y vuelve a intentarlo.")
        return False
    except Exception as e:  # pragma: no cover
        print(f"  [{BAD}] Error consultando VLC: {e}")
        return False

    try:
        root = ET.fromstring(raw.decode("utf-8", "replace"))

        def txt(tag: str, default: str = "") -> str:
            node = root.find(tag)
            return node.text if node is not None and node.text is not None else default

        state = (txt("state") or "stopped").strip()
        pos = float(txt("time") or 0)
        length = float(txt("length") or 0)
        vol = float(txt("volume") or 256)
        print(f"  [{OK}] VLC responde. estado={state}")
        print(f"        tiempo={int(pos)} s / duracion={int(length)} s · "
              f"volumen={round(vol / 256 * 100)}%")
        if state == "stopped":
            print(f"  [{WARN}] VLC esta en STOP: usa 'CARGAR ACTUAL' o "
                  f"'SINCRONIZAR AL PLAYOUT' en la pestana PLAYOUT.")
        return True
    except Exception as e:  # pragma: no cover
        print(f"  [{BAD}] Respuesta de VLC ilegible: {e}")
        return False


def check_panel(cfg: dict, timeout: float = 3.0) -> None:
    port = int((cfg.get("port") or 8088))
    url = f"http://127.0.0.1:{port}/api/vlc/status"
    print("— Panel TVPlayout —")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  [{WARN}] No pude consultar {url} ({e}).")
        print("        ?Esta el panel abierto (INICIAR_TVPLAYOUT.bat)?")
        return
    ready = bool(data.get("ready"))
    print(f"  [{_mark(ready)}] /api/vlc/status -> ready={ready} "
          f"state={data.get('state')!r} kind={data.get('kind')!r}")
    if data.get("error"):
        print(f"        error: {data['error']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnostico VLC <-> TVPlayout")
    ap.add_argument("--config", help="Ruta a config.json (por defecto el del repo)")
    ap.add_argument("--watch", action="store_true", help="Repetir cada 3 s")
    ap.add_argument("--panel", action="store_true", help="Consultar tambien al panel")
    ap.add_argument("--timeout", type=float, default=3.0, help="Timeout HTTP (s)")
    args = ap.parse_args()

    while True:
        cfg = load_config(args.config)
        print("=" * 62)
        check_config(cfg)
        check_exe(cfg)
        http_ok = check_http(cfg, timeout=args.timeout)
        if args.panel:
            check_panel(cfg, timeout=args.timeout)
        print("=" * 62)
        if not args.watch:
            return 0 if http_ok else 1
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
