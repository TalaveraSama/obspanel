# TVPlayout VLC PRO — Playout 24/7 con VLC (sin OBS)

Panel de automatización para un canal de TV: el **Scheduler** es la fuente de
verdad y **VLC (libvlc)** es el reproductor. Ya **no se usa OBS** para
reproducir: cada película se carga directamente desde su archivo original en
una instancia de VLC controlada por el panel.

## Reproducción
- Las películas se cargan desde su ruta original en **VLC** (libvlc), con
  ventana en pantalla completa si está configurado.
- El reloj del Scheduler manda: cuando llega la hora de la **siguiente
  película**, el motor la carga y la posiciona en el punto correcto.
- Si VLC queda vacío, termina o se cae, el motor lo vuelve a cargar solo con
  el evento que debe estar al aire.
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
