# TVPlayout VLC PRO — Playout 24/7 con VLC (app VLC + OBS por captura de ventana)

Panel de automatización para un canal de TV: el **Scheduler** es la fuente de
verdad y **VLC es el reproductor**. El panel controla el **VLC que ya tienes
instalado** (vlc.exe) por su interfaz HTTP: le carga cada película, lo
pausa/busca y maneja las tandas, pero la **ventana de VLC es única y nunca se
cierra** al cambiar de contenido.

> **OBS:** OBS ya **no reproduce** ni usa fuentes VLC: solo **captura la
> ventana de VLC** ("Captura de ventana" → *VLC media player*). Como la ventana
> no se recrea en cada película, la captura nunca pierde el objetivo ni se va a
> negro. Si prefieres la ventana embebida antigua, en **⚙️ AJUSTES VLC** puedes
> cambiar el modo a `libvlc`.

## Reproducción
- **Modo app (recomendado):** el panel arranca el VLC instalado con
  `--extraintf=http` (puerto por defecto 9099) y lo controla por HTTP. Una
  sola ventana persistente → OBS la captura por ventana.
- **Modo libvlc (respaldo):** ventana embebida vía python-vlc. No requiere
  configurar OBS por ventana, pero la ventana es del panel.
- Las películas se cargan desde su ruta original en **VLC**, con ventana en
  pantalla completa si está configurado.
- El reloj del Scheduler manda: cuando llega la hora de la **siguiente
  película**, el motor la carga y la posiciona en el punto correcto.
- Los botones **⏭ SIGUIENTE** y **⏮ ANTERIOR** cambian de película **al
  instante** (desde el inicio) y la mantienen al aire hasta que el Scheduler
  alcanza su hora; en ese momento la entrega es **sin corte** (no se recarga).
  Las tandas que cayeran en medio del salto se dan por emitidas.
- Si una película termina antes de su ventana programada (archivo más corto),
  el motor **encadena solo con la siguiente** para no dejar negro; si termina
  justo al filo del cambio, espera sin recargar (evita parpadeos).
- Si VLC queda vacío, termina o se cae, el motor lo vuelve a cargar solo con
  el evento que debe estar al aire.
- La reanudación tras una tanda usa la opción `:start-time` de libvlc más un
  seek verificado, para que la película nunca vuelva desde 0.
- No se genera ningún playlist por HTTP ni se remuxan películas.
- FFmpeg/ffprobe se usa únicamente para analizar la biblioteca.

## Tandas comerciales
- Los eventos `COMMERCIAL` (manuales o generados por AUTO_ADS) interrumpen la
  película en su posición **real** de VLC y, al terminar la tanda, la película
  se reanuda exactamente en ese punto.
- En **TANDAS** puedes activar el refresco automático (AUTO_ADS) y usar
  **✂️ CORTAR A TANDA AHORA** / **⏭ SALTAR TANDA**.
- Si el panel se reinicia a mitad de tanda, la reanudación se calcula restando
  el tiempo de comerciales ya emitidos al cursor del Scheduler.

## Audio / Subtítulos
- Selección por evento (pista de audio y subtítulo interno o SRT externo).
- Los SRT externos se convierten a una copia UTF-8 en `cache/subtitles` y se
  adjuntan a VLC al reproducir; el archivo original nunca se modifica.

## Requisitos
- Windows (recomendado) con **VLC instalado** (libvlc).
- Python 3.11–3.13. El panel detecta libvlc automáticamente (registro/carpetas
  típicas). Si no la encuentra, indícala en **⚙️ AJUSTES VLC → Carpeta libvlc**
  (p. ej. `C:\Program Files\VideoLAN\VLC`).

## Inicio
Ejecuta `INICIAR_TVPLAYOUT.bat`. Las dependencias se instalan desde
`requirements.txt` solo cuando hacen falta. Abre el panel en
`http://127.0.0.1:8088` y pulsa **🚀 INICIAR VLC** (o PREPARAR VLC) para
arrancar el reproductor.
