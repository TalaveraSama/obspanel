# 🎬 Cómo usar TVPlayout con VLC (guía paso a paso)

Esta guía es para dejar el canal al aire usando **el VLC que ya tienes instalado**
como reproductor. El panel no reproduce nada por sí solo: **el Scheduler manda y
VLC reproduce**.

> **Resumen en 4 pasos:** instalas VLC → abres el panel → en **⚙️ AJUSTES VLC**
> dejas el modo **App VLC instalada** y guardas → pulsas **🚀 INICIAR VLC**.
> Después, en OBS agregas **Captura de ventana → “VLC media player”**.

---

## 1. Requisitos

| Qué | Para qué | Notas |
|---|---|---|
| **VLC 3.x (64 bits)** | Es el reproductor del canal | https://www.videolan.org/vlc/ · instalación normal |
| **Python 3.11 – 3.13** | Corre el panel | Marca *“Add python.exe to PATH”* al instalarlo |
| **OBS** (opcional) | Captura la ventana de VLC | Solo si vas a emitir/ grabar por OBS |

En modo **app** (el recomendado) **no necesitas `python-vlc`**: el panel habla con
VLC por su **interfaz HTTP**. `python-vlc` solo hace falta si usas el modo
`libvlc` (respaldo, ventana embebida).

---

## 2. Arrancar el panel

1. Doble clic en **`INICIAR_TVPLAYOUT.bat`**.
   - La primera vez crea `.venv` e instala las dependencias (`requirements.txt`).
2. Abre en tu navegador: **http://127.0.0.1:8088**

---

## 3. Configurar VLC (pestaña ⚙️ AJUSTES VLC)

Ve a **⚙️ AJUSTES VLC** y deja esto:

| Campo | Valor recomendado | Explicación |
|---|---|---|
| **Tipo de reproductor** | `App VLC instalada (recomendado · ventana fija para OBS)` | Una sola ventana de VLC que **nunca se cierra** → OBS la captura sin perderla |
| **Ruta a vlc.exe (app)** | vacío o `C:\Program Files\VideoLAN\VLC\vlc.exe` | Se detecta solo; ponlo a mano si tu VLC está en otra carpeta |
| **Puerto control HTTP (app)** | `9099` | Por donde el panel le da órdenes a VLC (play/pause/seek/cargar) |
| **Contraseña HTTP (app)** | `tvplayout` | La usa el panel para autenticarse contra VLC. Cámbiala si quieres (debe coincidir con la que arranca VLC) |
| **Pantalla completa** | Activado | VLC arranca en full screen en el monitor principal |
| **Volumen maestro (0-200)** | `100` | 100 = volumen normal de VLC |
| **Cache de red (ms)** | `300` (sube a `1000–3000` si lees desde red/NAS) | Evita cortes si la película viene de un disco de red |
| **Idioma audio / subs (auto)** | `es,en,spa` | Preferencia de pistas al abrir cada película |
| **Carpeta libvlc** | solo para modo `libvlc` | Ej.: `C:\Program Files\VideoLAN\VLC` |

Pulsa **💾 GUARDAR AJUSTES VLC**. Guardar reconstruye el reproductor (no cierra la
ventana de VLC que ya esté abierta).

### ¿Qué hace el panel exactamente? (modo app)

Arranca tu VLC con una interfaz HTTP de control:

```
vlc.exe --intf qt --extraintf http
        --http-host 127.0.0.1 --http-port 9099 --http-password tvplayout
        --no-http-ssl --no-video-title-show --no-keyboard-events --no-mouse-events
        --no-one-instance --network-caching=300
        --audio-language=es,en,spa --sub-language=es,en,spa
        --preferred-resolution=-1 --fullscreen
```

Y después solo le **ordena** cosas: `in_play` (cargar película), `pl_pause`,
`pl_play`, `seek`, `volume`. **Nunca cierra ni recrea la ventana** → la captura de
OBS se mantiene fija.

---

## 4. Encender el reproductor

En **⚙️ AJUSTES VLC → 🔌 Control del reproductor**:

- **🚀 INICIAR VLC** → lanza VLC y carga el evento que toca según el Scheduler.
- **⏹ DETENER REPRODUCCIÓN** → deja VLC en stop (la ventana sigue abierta).
- **🔎 REVISAR VLC** → muestra si detectó el ejecutable y el estado.

También, desde **▶ PLAYOUT**:

- **🔧 PREPARAR VLC** → asegura VLC arriba y cargado.
- **🎬 CARGAR ACTUAL** → carga la película programada para este momento.
- **⏱ SINCRONIZAR AL PLAYOUT** → recoloca VLC en el minuto exacto del Scheduler.
- **⏭ SIGUIENTE / ⏮ ANTERIOR** → cambio instantáneo de película.
- **✂️ TANDA AHORA / ⏭ SALTAR TANDA** → cortes comerciales.

---

## 5. Capturarlo en OBS (para emitir o grabar)

1. En OBS: **＋ → Captura de ventana** (Window Capture).
2. En *Ventana* elige **`VLC media player`** (la ventana real de VLC).
3. **No uses** “Fuente de vídeo VLC (VLC Video Source)” ni “Captura de pantalla”.
4. Si quieres que ocupe todo el cuadro, activa **Pantalla completa** en los ajustes
   VLC del panel y usa un lienzo/base de OBS con la misma resolución que tu monitor
   (o captura en el monitor donde corre VLC).

Como la ventana de VLC **no se recrea** al cambiar de película, la fuente de OBS
nunca se queda en negro ni pierde el objetivo.

---

## 6. Comprobación manual (muy útil si algo falla)

Con VLC arrancado por el panel, abre en el navegador:

```
http://127.0.0.1:9099/requests/status.xml
```

- Usuario: *(vacío)*
- Contraseña: `tvplayout` (o la que pusiste en Ajustes)

Si ves un XML con `<state>playing</state>`, el control HTTP funciona. Si el
navegador no conecta, VLC no arrancó con la interfaz HTTP (revisa puerto y que no
haya otro VLC abierto ocupando el 9099).

También puedes correr el **doctor** incluido:

```bat
DIAGNOSTICO_VLC.bat
```

o desde una terminal:

```bash
python tools/vlc_doctor.py            # diagnóstico completo
python tools/vlc_doctor.py --watch    # refrescando cada 3 s
python tools/vlc_doctor.py --panel    # además consulta /api/vlc/status del panel
```

---

## 7. Uso diario (flujo normal)

1. **📡 SCANNER** → agrega tus carpetas de películas y escanea.
2. **📅 SCHEDULER** → genera la semana o el mes (aleatorio/secuencial).
3. **▶ PLAYOUT** → **🔧 PREPARAR VLC** y a correr.
4. El motor:
   - carga la película siguiente a su hora,
   - la posiciona en el minuto correcto si entraste tarde,
   - intercala **tandas** y reanuda la película en el punto exacto,
   - encadena la siguiente película si el archivo termina antes de su ventana,
   - reintenta solo si VLC se cae o queda vacío.
5. **📢 TANDAS** → activa AUTO_ADS para que las comerciales se generen solas.

---

## 8. Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| “No se encontró la app VLC instalada” | Ruta de `vlc.exe` mal detectada | Escribe la ruta completa en **Ruta a vlc.exe** y guarda |
| VLC no aparece / no responde | Otro VLC abierto en el puerto 9099 | Cierra todos los VLC y vuelve a **INICIAR VLC**; o cambia el **Puerto control HTTP** (p. ej. 9098) |
| El panel dice “VLC no responde” pero VLC está abierto | Ese VLC no se lanzó con `--extraintf=http` (lo abriste tú a mano) | Cierra ese VLC y usa **🚀 INICIAR VLC** desde el panel |
| OBS se queda en negro al cambiar de película | Estás capturando una fuente VLC de OBS o una ventana que se recrea | Usa **Captura de ventana → “VLC media player”** |
| Se corta la imagen leyendo desde NAS/red | Poca caché | Sube **Cache de red** a `1000–3000` ms y guarda |
| Audio en otro idioma | Preferencia de pistas | Ajusta **Idioma audio (auto)**, p. ej. `spa,es,en`; o elige la pista al aire en PLAYOUT |
| No salen subtítulos | Sin SRT junto al vídeo o pista apagada | Deja el `.srt` junto a la película con el mismo nombre (`Pelicula.mkv` + `Pelicula.srt`); selecciónalo en **💬 SUBTÍTULOS** o al aire |
| VLC arranca minimizado / en otro monitor | Full screen en monitor principal | Mueve VLC al monitor que quieras y desactiva **Pantalla completa** si prefieres ventana |
| La película vuelve desde 0 tras una tanda | Seek no confirmado | Usa **⏱ SINCRONIZAR AL PLAYOUT**; revisa que el archivo no esté en una ruta de red lenta |
| Firewall pregunta al arrancar | Interfaz HTTP local | Permite acceso **privado/local** para VLC (solo escucha en 127.0.0.1) |

### Modo respaldo: `libvlc` (ventana embebida)

Si por lo que sea no quieres usar la app VLC, en **⚙️ AJUSTES VLC** cambia
**Tipo de reproductor** a `libvlc embebido`, indica la **Carpeta libvlc**
(`C:\Program Files\VideoLAN\VLC`) y guarda. Requiere `python-vlc` y crea una
ventana propia del panel (puede cambiar de handle al cambiar de película, por eso
el modo **app** es el recomendado).

---

## 9. Nota técnica (arreglo incluido en esta rama)

El arranque de la app VLC (`engines/vlc_player.py → VlcHttpPlayer._launch_vlc`)
usaba una variable `v` que no existía en su ámbito: lanzaba
`NameError: name 'v' is not defined` y **el VLC instalado nunca llegaba a
arrancar** en modo app. Ya está corregido y cubierto por pruebas en
`tests/test_vlc_http_player.py`. Si venías de una versión anterior y el botón
**🚀 INICIAR VLC** te fallaba, actualiza a esta rama y vuelve a intentarlo.
