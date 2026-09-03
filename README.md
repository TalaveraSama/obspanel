# TVPlayout 15.8 — OBS/VLC Optimizado

Build basada en TVPlayout 15.8 para playout 24/7 con OBS WebSocket y **VLC Video Source**.

## Reproducción
- Las películas se cargan directamente desde su ruta original en la fuente VLC de OBS.
- Scheduler → VLC → OBS se conserva.
- No se genera ni sirve ningún playlist de reproducción por HTTP.
- No hay motor de segmentos ni procesos de FFmpeg para reproducción.
- FFmpeg/ffprobe se usa únicamente para analizar la biblioteca cuando es necesario.

## TMDB
- TMDB descarga únicamente el **poster** de la película.
- No se consultan ni descargan logos o backdrops.
- `RESETEAR TMDB + REESCANEAR` limpia las coincidencias y vuelve a buscar.

## OBS
- Se conserva la fuente VLC configurada en Settings.
- Al iniciar, se desactivan las fuentes antiguas `Program live` conocidas, sin borrarlas.
- Esto evita que una configuración OBS anterior siga intentando reproducir una fuente obsoleta.

## Inicio
Ejecuta `INICIAR_TVPLAYOUT.bat`. Las dependencias se instalan desde `requirements.txt` solo cuando hacen falta.
