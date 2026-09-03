import asyncio, json, os, random, sqlite3, subprocess, shutil, threading, time, re, hashlib, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, date, time as dtime, timedelta
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# Motor VLC directo (ya no se usa OBS WebSocket). La importación es opcional:
# si falta python-vlc/libvlc el panel sigue vivo en modo degradado.
from engines.vlc_player import VLC_BINDINGS_AVAILABLE, build_player, resolve_vlc
from engines.playout_engine import PlayoutEngine

BASE=Path(__file__).resolve().parent; CFG_PATH=BASE/"config.json"; CFG=json.loads(CFG_PATH.read_text(encoding="utf-8"))
CFG.setdefault("vlc", {})
CFG["vlc"].setdefault("enabled", True)
CFG["vlc"].setdefault("source", "VLC")          # compat: ya no es fuente de OBS
CFG["vlc"].setdefault("lib_dir", "")            # carpeta de libvlc (opcional)
CFG["vlc"].setdefault("path", "")               # vlc.exe (detección manual opcional)
CFG["vlc"].setdefault("fullscreen", True)
CFG["vlc"].setdefault("volume", 100)
CFG["vlc"].setdefault("network_caching", 300)
CFG["vlc"].setdefault("audio_language", "es,en,spa")
CFG["vlc"].setdefault("sub_language", "es,en,spa")
CFG["vlc"].setdefault("loop", False)
CFG["vlc"].setdefault("shuffle", False)
CFG.setdefault("auto_ads", {})
CFG["auto_ads"].setdefault("min_program_tail_seconds", 90)
CFG.setdefault("host", "127.0.0.1")
CFG.setdefault("port", 8088)
CFG.setdefault("channel", {"name": "MOVIES HD", "source": "VLC"})
CFG["channel"].setdefault("source", CFG["vlc"]["source"])
CFG.setdefault("title_overlay", {})
CFG.setdefault("logo", {})
CFG["title_overlay"].setdefault("enabled", False)
CFG["title_overlay"].setdefault("scene", "")
CFG["title_overlay"].setdefault("source", "")
CFG["title_overlay"].setdefault("show_during_ads", False)
CFG["title_overlay"].setdefault("interval_minutes", 15)
CFG["title_overlay"].setdefault("show_seconds", 8)
CFG["title_overlay"].setdefault("wrap_chars", 20)
CFG["title_overlay"].setdefault("mode", "gdi")
CFG["title_overlay"].setdefault("template", 3)
CFG["title_overlay"].setdefault("color1", "#FFFFFF")
CFG["title_overlay"].setdefault("color2", "#00A8FF")
CFG["title_overlay"].setdefault("poster_source", "")
CFG["title_overlay"].setdefault("logo_source", "")
CFG["title_overlay"].setdefault("poster_width", 180)
CFG["title_overlay"].setdefault("poster_height", 260)
CFG["title_overlay"].setdefault("poster_x", 80)
CFG["title_overlay"].setdefault("poster_y", 820)
CFG["title_overlay"].setdefault("poster_keep_ratio", True)
CFG["title_overlay"].setdefault("poster_crop", False)
CFG.setdefault("tmdb", {})
CFG["tmdb"].setdefault("enabled", False)
CFG["tmdb"].setdefault("auto_enrich", False)
CFG["tmdb"].setdefault("api_key", "")
CFG["tmdb"].setdefault("language", "es-MX")
CFG["tmdb"].setdefault("region", "MX")
CFG["tmdb"].setdefault("last_error", "")
DB=BASE/"tvplayout.db"; EXTS={".mkv",".mp4",".m4v",".mov",".avi",".webm",".ts",".m2ts",".mts"}
STATE={"vlc_ready":False,"vlc_error":None,"vlc_state":"idle",
       "scanner":{"running":False,"paused":False,"folder":"","found":0,"analyzed":0,"pending":0,"errors":[],"started":None,"finished":None},
       "current":None,"next":None,"upcoming":[],"obs_connected":False,
       "last_error":None,"ad_break":False,"mode":"IDLE","interrupted_title":None}

# ---------------------------------------------------------------------------
# Motor VLC directo (sustituye a OBS WebSocket)
# ---------------------------------------------------------------------------
PLAYER=None
PLAYER_LOCK=threading.Lock()
ENGINE=None
ENGINE_LOCK=threading.Lock()
VLC_INFO={"ok":False,"error":"","lib_dir":"","lib_file":"","module_available":False,"vlc_version":""}

def get_player():
    """Devuelve (y arranca la primera vez) el reproductor VLC del playout."""
    global PLAYER
    if PLAYER is None:
        with PLAYER_LOCK:
            if PLAYER is None:
                PLAYER=build_player(CFG)
                ok,err=PLAYER.connect()
                VLC_INFO.update({"ok":bool(ok),"error":err or ""})
    return PLAYER

def get_engine():
    global ENGINE
    if ENGINE is None:
        with ENGINE_LOCK:
            if ENGINE is None:
                ENGINE=PlayoutEngine(CFG, DB, get_player(), base_dir=BASE)
    return ENGINE

def vlc_info():
    r=resolve_vlc(CFG)
    VLC_INFO.update({"module_available":bool(r.get("module_available",False)),
                     "lib_dir":r.get("lib_dir","") or VLC_INFO.get("lib_dir",""),
                     "lib_file":r.get("lib_file","") or VLC_INFO.get("lib_file",""),
                     "vlc_version":r.get("vlc_version","") or VLC_INFO.get("vlc_version","")})
    if r.get("error") and not VLC_INFO.get("ok"):
        VLC_INFO["error"]=r["error"]
    return dict(VLC_INFO)

app=FastAPI(title="TVPlayout VLC PRO · Playout por VLC")
templates=Jinja2Templates(directory=str(BASE/"templates"))

def safe_fromjson(value):
    if not value:
        return []
    try:
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else []
    except Exception:
        return []

templates.env.filters["fromjson"]=safe_fromjson
(BASE/"cache"/"tmdb"/"posters").mkdir(parents=True,exist_ok=True)
(BASE/"cache"/"tmdb"/"logos").mkdir(parents=True,exist_ok=True)
(BASE/"cache"/"tmdb"/"backdrops").mkdir(parents=True,exist_ok=True)
(BASE/"cache"/"tmdb"/"metadata").mkdir(parents=True,exist_ok=True)
app.mount("/static",StaticFiles(directory=str(BASE/"static")),name="static")
app.mount("/tmdb-cache",StaticFiles(directory=str(BASE/"cache"/"tmdb")),name="tmdb-cache")
SCAN_LOCK=asyncio.Lock(); SCAN_TASK=None
GENERATION_TASK=None
GENERATION_STATE={"running":False,"type":"","month":"","start":"","message":"","result":None,"error":None}
TMDB_TASK=None
TMDB_RUN_EVENT=threading.Event()
TMDB_LOCK=threading.Lock()
TMDB_STATS={"running":False,"processed":0,"found":0,"not_found":0,"errors":0,"pending":0,"last_title":"","last_error":""}

def db():
 c=sqlite3.connect(DB,timeout=10)
 c.row_factory=sqlite3.Row
 c.execute("PRAGMA busy_timeout=10000")
 c.execute("PRAGMA synchronous=NORMAL")
 return c

def configure_sqlite():
    """Configure SQLite once at startup for concurrent UI/scanner access."""
    c=sqlite3.connect(DB, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.commit()
    finally:
        c.close()

def clean_display_title(value):
    """Create a broadcast-friendly title from a filename/title.
    Removes extension, bracket metadata, release year and common release tags.
    IMDb can later replace this with an official title when configured.
    """
    x=Path(str(value or "")).stem.strip()
    x=re.sub(r"[._]+", " ", x)
    x=re.sub(r"\[[^\]]*\]|\([^)]*?(?:1080p|2160p|720p|WEB[- ]?DL|WEBRip|BluRay|HDR|x264|x265|HEVC|AAC|DDP|DTS)[^)]*\)", " ", x, flags=re.I)
    x=re.sub(r"\b(19|20)\d{2}\b\s*$", "", x)
    x=re.sub(r"\s+(19|20)\d{2}\s*$", "", x)
    x=re.sub(r"\s+", " ", x).strip(" -_")
    return x or str(value or "").strip()

def tmdb_config():
    return CFG.get("tmdb", {})

def tmdb_api(path, params=None):
    cfg=tmdb_config(); key=str(cfg.get("api_key") or "").strip()
    if not key: raise RuntimeError("TMDB API Key no configurada.")
    q=dict(params or {})
    q.update({"api_key":key,"language":cfg.get("language","es-MX")})
    url="https://api.themoviedb.org/3"+path+"?"+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={"Accept":"application/json","User-Agent":"TVPlayout12.9"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def tmdb_image_download(path, target, size="w500"):
    if not path: return ""
    target=Path(target); target.parent.mkdir(parents=True,exist_ok=True)
    url=f"https://image.tmdb.org/t/p/{size}{path}"
    req=urllib.request.Request(url,headers={"User-Agent":"TVPlayout12.9"})
    with urllib.request.urlopen(req,timeout=25) as r:
        data=r.read()
    tmp=target.with_suffix(target.suffix+".tmp")
    tmp.write_bytes(data); tmp.replace(target)
    return str(target)

def tmdb_enrich_media(media_id):
    with TMDB_LOCK:
        return _tmdb_enrich_media_locked(media_id)

def _tmdb_enrich_media_locked(media_id):
    c=db(); m=c.execute("SELECT id,path,title FROM media WHERE id=?",(media_id,)).fetchone()
    if not m: c.close(); return {"status":"missing"}
    c.execute("""INSERT INTO tmdb_cache(media_id,status,updated_at) VALUES(?,?,?)
                 ON CONFLICT(media_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,error=''""",
              (media_id,"processing",datetime.now().isoformat(timespec="seconds")))
    c.commit(); c.close()
    try:
        query=clean_display_title(m["title"] or Path(m["path"]).stem)
        data=tmdb_api("/search/movie",{"query":query,"region":tmdb_config().get("region","MX"),"include_adult":"false"})
        results=data.get("results") or []
        norm=lambda s: re.sub(r"[^a-z0-9áéíóúüñ]+","",str(s).lower())
        exact=[x for x in results if norm(x.get("title",""))==norm(query)]
        movie=(exact or results)[0] if (exact or results) else None
        if not movie:
            c=db(); c.execute("""UPDATE tmdb_cache SET status='not_found',tmdb_title=?,error='',updated_at=? WHERE media_id=?""",
                              (query,datetime.now().isoformat(timespec="seconds"),media_id)); c.commit(); c.close()
            return {"status":"not_found","media_id":media_id,"query":query}
        tid=int(movie.get("id")); sid=str(tid)
        root=BASE/"cache"/"tmdb"; posters=root/"posters"; metadata=root/"metadata"
        posters.mkdir(parents=True,exist_ok=True); metadata.mkdir(parents=True,exist_ok=True)
        # TMDB integration intentionally downloads ONLY movie posters.
        poster_local=tmdb_image_download(movie.get("poster_path"),posters/(sid+".jpg"),"w500") if movie.get("poster_path") else ""
        rd=str(movie.get("release_date") or "")
        year=int(rd[:4]) if rd[:4].isdigit() else None
        c=db(); c.execute("""UPDATE tmdb_cache SET tmdb_id=?,tmdb_title=?,tmdb_original_title=?,tmdb_year=?,
            poster_path=?,poster_local=?,logo_path='',logo_local='',backdrop_path='',backdrop_local='',
            status='found',error='',updated_at=? WHERE media_id=?""",
            (tid,movie.get("title") or query,movie.get("original_title") or "",year,
             movie.get("poster_path") or "",poster_local,datetime.now().isoformat(timespec="seconds"),media_id))
        c.commit(); c.close()
        try:
            meta={"media_id":media_id,"tmdb_id":tid,"title":movie.get("title") or query,
                  "original_title":movie.get("original_title") or "","year":year,
                  "poster_local":poster_local,
                  "updated_at":datetime.now().isoformat(timespec="seconds")}
            (root/"metadata"/(str(media_id)+".json")).write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception:
            pass
        return {"status":"found","media_id":media_id,"tmdb_id":tid,"title":movie.get("title") or query,"poster_local":poster_local}
    except Exception as e:
        cfg=CFG["tmdb"]; cfg["last_error"]=str(e)
        save_cfg()
        c=db(); c.execute("""UPDATE tmdb_cache SET status='error',error=?,updated_at=? WHERE media_id=?""",
                          (str(e),datetime.now().isoformat(timespec="seconds"),media_id)); c.commit(); c.close()
        return {"status":"error","media_id":media_id,"error":str(e)}

def tmdb_pending_ids(limit=1):
    c=db()
    rows=c.execute("""SELECT m.id FROM media m LEFT JOIN tmdb_cache t ON t.media_id=m.id
                      WHERE m.enabled=1 AND (t.media_id IS NULL OR t.status IN ('pending','error'))
                      ORDER BY m.id LIMIT ?""",(int(limit),)).fetchall()
    c.close()
    return [int(r["id"]) for r in rows]

def tmdb_stats():
    c=db()
    total=c.execute("SELECT COUNT(*) FROM media WHERE enabled=1").fetchone()[0]
    found=c.execute("SELECT COUNT(*) FROM tmdb_cache WHERE status='found'").fetchone()[0]
    pending=c.execute("""SELECT COUNT(*) FROM media m LEFT JOIN tmdb_cache t ON t.media_id=m.id
                         WHERE m.enabled=1 AND (t.media_id IS NULL OR t.status='pending')""").fetchone()[0]
    nf=c.execute("SELECT COUNT(*) FROM tmdb_cache WHERE status='not_found'").fetchone()[0]
    err=c.execute("SELECT COUNT(*) FROM tmdb_cache WHERE status='error'").fetchone()[0]
    processing=c.execute("SELECT COUNT(*) FROM tmdb_cache WHERE status='processing'").fetchone()[0]
    c.close()
    return {"total":total,"found":found,"pending":pending,"not_found":nf,"errors":err,"processing":processing,
            "running":bool(TMDB_STATS.get("running")),"last_title":TMDB_STATS.get("last_title",""),
            "last_error":TMDB_STATS.get("last_error") or CFG.get("tmdb",{}).get("last_error","")}

def tmdb_display_title(media_id, fallback):
    c=db(); r=c.execute("SELECT tmdb_title FROM tmdb_cache WHERE media_id=? AND status='found'",(media_id,)).fetchone(); c.close()
    return (r["tmdb_title"] if r and r["tmdb_title"] else fallback)

def tmdb_media_assets(media_id):
    c=db(); r=c.execute("SELECT poster_local,tmdb_title FROM tmdb_cache WHERE media_id=? AND status='found'",(media_id,)).fetchone(); c.close()
    return dict(r) if r else {}

async def tmdb_worker():
    while True:
        try:
            cfg=tmdb_config()
            should_run=bool(cfg.get("enabled") and (cfg.get("auto_enrich") or TMDB_RUN_EVENT.is_set()))
            if should_run:
                ids=await asyncio.to_thread(tmdb_pending_ids,1)
                if ids:
                    TMDB_STATS["running"]=True
                    result=await asyncio.to_thread(tmdb_enrich_media,ids[0])
                    TMDB_STATS["processed"]+=1
                    TMDB_STATS["last_title"]=result.get("title") or result.get("query") or str(ids[0])
                    st=result.get("status")
                    if st=="found":
                        TMDB_STATS["found"]+=1
                        cur=STATE.get("current") or {}
                        if int(cur.get("media_id") or 0)==int(ids[0]):
                            # El título del evento al aire se actualiza vía
                            # tmdb_display_title en la siguiente lectura.
                            pass
                    elif st=="not_found": TMDB_STATS["not_found"]+=1
                    elif st=="error":
                        TMDB_STATS["errors"]+=1; TMDB_STATS["last_error"]=result.get("error","")
                    await asyncio.sleep(0.35)
                else:
                    TMDB_RUN_EVENT.clear()
                    TMDB_STATS["running"]=False
                    await asyncio.sleep(1)
            else:
                TMDB_STATS["running"]=False
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            TMDB_STATS["last_error"]=str(e); TMDB_STATS["errors"]+=1
            await asyncio.sleep(2)

def normalize_existing_titles():
    c=db()
    rows=c.execute("SELECT id,title FROM media").fetchall()
    changed=0
    for r in rows:
        clean=clean_display_title(r["title"])
        if clean and clean != r["title"]:
            c.execute("UPDATE media SET title=? WHERE id=?",(clean,r["id"]))
            changed+=1
    if changed: c.commit()
    c.close()
    return changed

def init_db():
 configure_sqlite()
 c=db(); c.executescript("""
 CREATE TABLE IF NOT EXISTS folders(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE,name TEXT,enabled INTEGER DEFAULT 1,category TEXT DEFAULT 'Movie',recursive INTEGER DEFAULT 1);
 CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE,title TEXT,duration REAL DEFAULT 0,width INTEGER DEFAULT 0,height INTEGER DEFAULT 0,audio_json TEXT DEFAULT '[]',subs_json TEXT DEFAULT '[]',category TEXT DEFAULT 'Movie',enabled INTEGER DEFAULT 1,folder_id INTEGER,size INTEGER DEFAULT 0,mtime REAL DEFAULT 0);
 CREATE TABLE IF NOT EXISTS imdb_cache(id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER UNIQUE,imdb_id TEXT,imdb_title TEXT,imdb_year INTEGER,updated_at TEXT); 
 CREATE TABLE IF NOT EXISTS tmdb_cache(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   media_id INTEGER UNIQUE,
   tmdb_id INTEGER,
   tmdb_title TEXT,
   tmdb_original_title TEXT,
   tmdb_year INTEGER,
   poster_path TEXT,
   poster_local TEXT,
   logo_path TEXT,
   logo_local TEXT,
   backdrop_path TEXT,
   backdrop_local TEXT,
   status TEXT DEFAULT 'pending',
   error TEXT DEFAULT '',
   updated_at TEXT
 );
 CREATE TABLE IF NOT EXISTS playlist(id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER,position INTEGER,audio_index INTEGER DEFAULT 0,subtitle_index INTEGER DEFAULT -1,kind TEXT DEFAULT 'PROGRAM',enabled INTEGER DEFAULT 1);
 CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER,start_at TEXT,end_at TEXT,audio_index INTEGER DEFAULT 0,subtitle_index INTEGER DEFAULT -1,kind TEXT DEFAULT 'PROGRAM',status TEXT DEFAULT 'scheduled',source TEXT DEFAULT 'MANUAL',day_key TEXT DEFAULT '',generated_run TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS asrun(id INTEGER PRIMARY KEY AUTOINCREMENT,event_time TEXT,media_id INTEGER,title TEXT,kind TEXT,audio_index INTEGER DEFAULT 0,subtitle_index INTEGER DEFAULT -1,duration REAL DEFAULT 0,status TEXT DEFAULT 'PLAYED');
 CREATE TABLE IF NOT EXISTS playback_state(
   id INTEGER PRIMARY KEY CHECK(id=1),
   schedule_id INTEGER,
   media_id INTEGER,
   title TEXT,
   path TEXT,
   source TEXT,
   scheduled_start TEXT,
   scheduled_end TEXT,
   duration REAL DEFAULT 0,
   position_ms INTEGER DEFAULT 0,
   last_checkpoint TEXT,
   state TEXT DEFAULT 'idle',
   audio_index INTEGER DEFAULT 0,
   subtitle_index INTEGER DEFAULT -1
 );
 """); c.commit(); c.close()

def save_cfg(): CFG_PATH.write_text(json.dumps(CFG,indent=2),encoding="utf-8")

def resolve_bin(name,configured):
 for p in [configured,shutil.which(name),rf"C:\ffmpeg\bin\{name}.exe",rf"C:\Program Files\ffmpeg\bin\{name}.exe",str(BASE/"bin"/f"{name}.exe")]:
  if p:
   try:
    if Path(p).exists(): return str(Path(p))
   except: pass
 return ""

def bins():
 ff=resolve_bin("ffmpeg",CFG.get("ffmpeg","")); fp=resolve_bin("ffprobe",CFG.get("ffprobe","")); return ff,fp

def ffprobe(path):
 ff,fp=bins()
 if not fp: raise RuntimeError("FFprobe no encontrado. Instala FFmpeg o coloca ffprobe.exe en TVPlayout\\bin.")
 p=subprocess.run([fp,"-v","error","-show_streams","-show_format","-of","json",str(path)],
                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding="utf-8",errors="replace",timeout=90)
 if p.returncode: raise RuntimeError(p.stderr[-1800:])
 d=json.loads(p.stdout); fmt=d.get("format",{}); v=next((s for s in d.get("streams",[]) if s.get("codec_type")=="video"),{})
 aud=[]
 for n,s in enumerate([x for x in d.get("streams",[]) if x.get("codec_type")=="audio"]):
  t=s.get("tags",{}); aud.append({"ordinal":n,"stream_index":s.get("index"),"language":t.get("language","und"),"title":t.get("title",""),"codec":s.get("codec_name","")})
 subs=[]
 for n,s in enumerate([x for x in d.get("streams",[]) if x.get("codec_type")=="subtitle"]):
  t=s.get("tags",{}); subs.append({"ordinal":n,"stream_index":s.get("index"),"language":t.get("language","und"),"title":t.get("title",""),"codec":s.get("codec_name","")})
 return float(fmt.get("duration") or 0),int(v.get("width") or 0),int(v.get("height") or 0),aud,subs

def _subtitle_lang(name):
    n=Path(str(name)).stem.lower()
    for code,label in [
        ("spa","es"),("spanish","es"),("es","es"),
        ("eng","en"),("english","en"),("en","en"),
        ("fra","fr"),("fre","fr"),("fr","fr"),
        ("por","pt"),("pt","pt"),
        ("ita","it"),("it","it"),("deu","de"),("ger","de"),("de","de"),
    ]:
        if re.search(rf"(^|[._ -]){re.escape(code)}([._ -]|$)",n):
            return label
    return "und"

def external_subs(p):
    """Find external subtitle files next to the media and in subtitle folders."""
    p=Path(p)
    roots=[p.parent]
    for dname in ("Subs","subs","Subtitles","subtitles","Subtitle","subtitle"):
        d=p.parent/dname
        if d.is_dir():
            roots.append(d)
    # Also accept nested subtitle folders one level below the media directory.
    try:
        roots += [d for d in p.parent.iterdir() if d.is_dir() and d.name.lower() in {"subs","subtitles","subtitle"}]
    except Exception:
        pass

    seen=set(); candidates=[]
    exts={".srt",".ass",".ssa",".vtt"}
    for root in roots:
        try:
            files=sorted(x for x in root.iterdir() if x.is_file() and x.suffix.lower() in exts)
        except Exception:
            files=[]
        for sub in files:
            try: key=str(sub.resolve()).lower()
            except Exception: key=str(sub).lower()
            if key in seen: continue
            seen.add(key)
            candidates.append({
                "path":str(sub),
                "language":_subtitle_lang(sub.name),
                "title":sub.name,
                "codec":sub.suffix[1:].lower(),
                "forced":"forced" in sub.stem.lower(),
                "external":True
            })
    # Prefer language-coded files but do not require the media basename to match.
    return sorted(candidates, key=lambda x: (0 if x["language"]=="es" else 1 if x["language"]=="en" else 2, x["title"].lower()))

def scan_folder(fid):
 c=db(); f=c.execute("SELECT * FROM folders WHERE id=?",(fid,)).fetchone(); c.close()
 if not f: return
 root=Path(f["path"]); S=STATE["scanner"]; S.update(running=True,paused=False,folder=str(root),found=0,analyzed=0,pending=0,errors=[],started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),finished=None)
 if not root.exists(): S["errors"]=[f"Carpeta no encontrada: {root}"]; S["running"]=False; return
 files=[]
 for cur,dirs,names in os.walk(root,topdown=True):
  dirs[:]=[d for d in dirs if d not in {"$RECYCLE.BIN","System Volume Information"}]
  if not f["recursive"]: dirs[:]=[]
  for n in names:
   p=Path(cur)/n
   if p.suffix.lower() in EXTS: files.append(p)
 S["found"]=len(files); S["pending"]=len(files)
 batch=[]
 for p in files:
  while S["paused"]: awaitable_sleep(0.25)
  try:
   st=p.stat()
   c=db(); old=c.execute("SELECT size,mtime FROM media WHERE path=?",(str(p),)).fetchone(); c.close()
   if old and old["size"]==st.st_size and old["mtime"]==st.st_mtime:
    S["analyzed"]+=1; S["pending"]-=1; continue
   dur,w,h,a,subs=ffprobe(p); subs += external_subs(p)
   batch.append((str(p),clean_display_title(p.stem),dur,w,h,json.dumps(a,ensure_ascii=False),json.dumps(subs,ensure_ascii=False),f["category"],f["id"],st.st_size,st.st_mtime))
   S["analyzed"]+=1
   if len(batch)>=5:
    c=db()
    c.executemany("""INSERT INTO media(path,title,duration,width,height,audio_json,subs_json,category,folder_id,size,mtime)
      VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET title=excluded.title,duration=excluded.duration,width=excluded.width,height=excluded.height,audio_json=excluded.audio_json,subs_json=excluded.subs_json,category=excluded.category,folder_id=excluded.folder_id,size=excluded.size,mtime=excluded.mtime""",batch)
    c.commit(); c.close(); batch=[]
  except Exception as e:
   S["errors"].append(f"{p}: {e}")
  finally:
   S["pending"]=max(0,S["pending"]-1)
 if batch:
  try:
   c=db(); c.executemany("""INSERT INTO media(path,title,duration,width,height,audio_json,subs_json,category,folder_id,size,mtime)
     VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET title=excluded.title,duration=excluded.duration,width=excluded.width,height=excluded.height,audio_json=excluded.audio_json,subs_json=excluded.subs_json,category=excluded.category,folder_id=excluded.folder_id,size=excluded.size,mtime=excluded.mtime""",batch); c.commit(); c.close()
  except Exception as e: S["errors"].append(f"DB final: {e}")
 S["running"]=False; S["finished"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def awaitable_sleep(seconds):
 time.sleep(seconds)

def find_current_scheduled_event(now=None):
    now = now or datetime.now()
    stamp=now.strftime("%Y-%m-%dT%H:%M:%S")
    c=db()
    r=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                   FROM schedule s JOIN media m ON m.id=s.media_id
                   WHERE s.start_at<=? AND s.end_at>? 
                     AND s.status IN ('scheduled','playing')
                   ORDER BY s.start_at DESC,s.id DESC LIMIT 1""",(stamp,stamp)).fetchone()
    c.close()
    return r

def find_next_scheduled_event(now=None):
    """Return the first scheduled event strictly after the current event/time."""
    now=now or datetime.now()
    stamp=now.strftime("%Y-%m-%dT%H:%M:%S")
    c=db()
    r=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                   FROM schedule s JOIN media m ON m.id=s.media_id
                   WHERE s.status='scheduled' AND s.start_at>?
                   ORDER BY s.start_at,s.id LIMIT 1""",(stamp,)).fetchone()
    c.close()
    return r

def find_upcoming_scheduled_events(now=None, limit=5):
    now=now or datetime.now()
    stamp=now.strftime("%Y-%m-%dT%H:%M:%S")
    c=db()
    rows=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                    FROM schedule s JOIN media m ON m.id=s.media_id
                    WHERE s.status='scheduled' AND s.start_at>?
                    ORDER BY s.start_at,s.id LIMIT ?""",(stamp,int(limit))).fetchall()
    c.close()
    return rows

VLC_STATUS_CACHE={"ts":0.0,"result":None}

def verify_vlc_output(force=False):
    """Estado real del reproductor VLC (ya no hay fuente dentro de OBS)."""
    now=time.monotonic()
    if not force and VLC_STATUS_CACHE["result"] is not None and now-float(VLC_STATUS_CACHE["ts"]) < 2:
        return dict(VLC_STATUS_CACHE["result"])
    source=vlc_source_name()
    result={"ready":False,"scene":None,"source":source,
            "connected":False,"kind":"libvlc","linked":True,"playlist":0,
            "error":None,"state":"idle","uri":"","position_ms":0,"length_ms":0}
    try:
        pl=get_player()
        ok,err=pl.connect()
        if not ok:
            result["error"]=err or "VLC no disponible"
            VLC_STATUS_CACHE.update({"ts":now,"result":dict(result)})
            return result
        snap=pl.snapshot()
        result["connected"]=bool(snap.available)
        result["ready"]=bool(snap.available and snap.has_input)
        result["playlist"]=1 if snap.has_input else 0
        result["error"]=snap.error or None
        result["state"]=snap.state
        result["uri"]=snap.uri
        result["position_ms"]=snap.position_ms
        result["length_ms"]=snap.length_ms
        if not result["connected"] and not snap.error:
            result["error"]="VLC no conectado"
    except Exception as e:
        result["error"]=str(e)
    STATE["vlc_ready"]=bool(result.get("ready"))
    STATE["vlc_error"]=result.get("error")
    VLC_STATUS_CACHE.update({"ts":now,"result":dict(result)})
    return result

def ensure_vlc_player():
    """Arranca/verifica el reproductor VLC y deja lista la salida de playout.

    (Nombre conservado por compatibilidad con los endpoints: ya no se prepara
    ninguna escena ni fuente dentro de OBS.)
    """
    source=vlc_source_name()
    result={"ok":False,"scene":None,"source":source,"created_scene":False,
            "created_source":False,"linked":True,"error":None}
    try:
        pl=get_player()
        ok,err=pl.connect()
        if not ok:
            result["error"]=err or "VLC no disponible."
            STATE["vlc_ready"]=False
            STATE["vlc_error"]=result["error"]
            return result
        result["ok"]=True
        STATE["vlc_ready"]=bool(pl.has_input())
        STATE["vlc_error"]=""
        # Si hay un evento al aire y VLC quedó vacío, dejarlo sonando.
        try:
            current=find_current_scheduled_event()
            if current and not pl.has_input():
                res=get_engine().play_scheduled_row(dict(current),0)
                STATE["vlc_ready"]=bool(res.get("ok"))
                STATE["vlc_error"]=res.get("error") or ""
        except Exception:
            pass
        result["status"]=verify_vlc_output(True)
        return result
    except Exception as e:
        STATE["vlc_ready"]=False
        STATE["vlc_error"]=str(e)
        result["error"]=str(e)
        return result

def vlc_source_name():
    # En la arquitectura VLC directo no hay "fuente": devolvemos la etiqueta
    # del reproductor para no romper campos de estado existentes.
    return "VLC"

def _vlc_action(action):
    """Acciones del operador: play/pause/stop/restart/next/previous."""
    eng=get_engine()
    if action in ("play","pause"):
        return eng.action_play_pause()
    if action=="stop":
        return eng.action_stop()
    if action=="restart":
        return eng.action_restart()
    if action=="next":
        return eng.action_next()
    if action=="previous":
        return eng.action_previous()
    return {"ok":False,"error":"Acción VLC no válida"}

def play_row(row, resume_ms=0):
    """Pone un evento al aire usando el reproductor VLC (sin OBS)."""
    row=dict(row)
    kind=str(row.get("kind") or "PROGRAM")
    if kind=="COMMERCIAL":
        res=get_engine().play_scheduled_row(row,int(resume_ms or 0))
    else:
        res=get_engine().play_scheduled_row(row,int(resume_ms or 0))
    if res.get("error") and not res.get("ok"):
        STATE["last_error"]=str(res["error"])
        raise RuntimeError(res["error"])
    return res

def generate_week(start_date, mode="random", category="Movie", avoid_repeat=True, week_no=1, month=""):
    start = date.fromisoformat(start_date) if isinstance(start_date,str) else start_date
    if month:
        y,m=map(int,month.split("-"))
        month_start=date(y,m,1)
        month_end=date(y+1,1,1) if m==12 else date(y,m+1,1)
        start=max(start,month_start)
        end=min(start+timedelta(days=7),month_end)
    else:
        end=start+timedelta(days=7)

    c=db()
    media=c.execute(
        "SELECT * FROM media WHERE enabled=1 AND duration>0 AND (?='' OR category=?)",
        (category,category)
    ).fetchall()
    if not media:
        c.close()
        raise RuntimeError("No hay medios disponibles para la categoría seleccionada.")

    # Only regenerate this automatic weekly block.
    c.execute(
        "DELETE FROM schedule WHERE source='AUTO_WEEKLY' AND status!='playing' AND day_key>=? AND day_key<?",
        (start.isoformat(),end.isoformat())
    )

    pool=[dict(x) for x in media]
    rng=random.Random(f"{start.isoformat()}-{time.time_ns()}")
    cycle=[]

    def take_random(remaining):
        nonlocal cycle
        fitting=[x for x in cycle if x["duration"]<=remaining] if cycle else []
        if fitting:
            row=rng.choice(fitting)
            cycle.remove(row)
            return row
        if not cycle:
            cycle=pool[:]
            rng.shuffle(cycle)
            fitting=[x for x in cycle if x["duration"]<=remaining]
            if fitting:
                row=rng.choice(fitting)
                cycle.remove(row)
                return row
        fitting=[x for x in pool if x["duration"]<=remaining]
        if not fitting:
            return None
        row=rng.choice(fitting)
        if row in cycle:
            cycle.remove(row)
        return row

    total=0
    total_seconds=0.0

    for day_offset in range((end-start).days):
        day=start+timedelta(days=day_offset)
        cur=datetime.combine(day,dtime(0,0))
        day_end=cur+timedelta(days=1)
        sequential_index=0

        while cur < day_end:
            remaining=(day_end-cur).total_seconds()

            if mode=="random":
                row=take_random(remaining)
            else:
                fitting=[x for x in pool if x["duration"]<=remaining]
                if not fitting:
                    break
                row=fitting[sequential_index % len(fitting)]
                sequential_index += 1

            if not row:
                break

            en=cur+timedelta(seconds=float(row["duration"]))

            c.execute(
                """INSERT INTO schedule
                (media_id,start_at,end_at,audio_index,subtitle_index,kind,status,source,day_key,generated_run)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    cur.strftime("%Y-%m-%dT%H:%M:%S"),
                    en.strftime("%Y-%m-%dT%H:%M:%S"),
                    0,-1,"PROGRAM","scheduled","AUTO_WEEKLY",
                    day.isoformat(),
                    f"{month or start.strftime('%Y-%m')}-W{week_no}"
                )
            )
            cur=en
            total += 1
            total_seconds += float(row["duration"])

    c.commit()
    c.close()
    return {
        "week":week_no,
        "start":start.isoformat(),
        "end":(end-timedelta(days=1)).isoformat(),
        "events":total,
        "hours":round(total_seconds/3600,2)
    }

def generate_month_weeks(month, mode="random", category="Movie", avoid_repeat=True):
    y,m=map(int,month.split("-"))
    month_start=date(y,m,1)
    month_end=date(y+1,1,1) if m==12 else date(y,m+1,1)

    results=[]
    cur=month_start
    week_no=1

    while cur<month_end:
        result=generate_week(
            cur,mode,category,avoid_repeat,
            week_no,month
        )
        results.append(result)
        cur += timedelta(days=7)
        week_no += 1

    return {
        "month":month,
        "weeks":results,
        "events":sum(x["events"] for x in results),
        "hours":round(sum(x["hours"] for x in results),2)
    }

async def engine():
    """Bucle de playout: el Scheduler manda; VLC reproduce (ya sin OBS).

    El motor nuevo (engines/playout_engine) decide cuándo cargar la película
    siguiente, interrumpir para tanda comercial y reanudar en el punto exacto.
    Aquí solo lo conducimos y reflejamos su estado en STATE para la UI.
    """
    eng=get_engine()
    try:
        verify_vlc_output(True)
    except Exception:
        pass
    while True:
        try:
            eng.tick()
        except Exception as e:
            STATE["last_error"]=f"playout: {e}"
        try:
            snap=eng.snapshot()
            STATE.update({
                "mode":snap.get("mode") or "IDLE",
                "current":snap.get("current"),
                "next":snap.get("next"),
                "upcoming":snap.get("upcoming") or [],
                "ad_break":bool(snap.get("ad_break")),
                "interrupted_title":snap.get("interrupted_title"),
            })
            p=snap.get("player") or {}
            # claves heredadas para no romper lecturas antiguas de la UI
            STATE["obs_connected"]=bool(p.get("available",False))
            STATE["vlc_ready"]=bool(p.get("available",False) and p.get("has_input",False))
            STATE["vlc_error"]=p.get("error") or ""
            STATE["vlc_state"]=p.get("state") or "idle"
            if p.get("position_ms") is not None and (STATE.get("current") or {}):
                try:
                    STATE["current"]["position_ms"]=int(p.get("position_ms") or 0)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.5)

async def ads_maintainer():
    """Mantiene insertadas las tandas AUTO_ADS de las próximas horas."""
    eng=get_engine()
    while True:
        try:
            eng.maintain_auto_ads()
        except Exception:
            pass
        await asyncio.sleep(45)

@app.on_event("startup")
async def startup():
    global TMDB_TASK
    init_db()
    normalize_existing_titles()
    # TMDB worker: /tmdb/run solo marca un Event; sin esta tarea nunca
    # se procesarían las búsquedas.
    if TMDB_TASK is None or TMDB_TASK.done():
        TMDB_TASK = asyncio.create_task(tmdb_worker())
    asyncio.create_task(engine())
    asyncio.create_task(ads_maintainer())

@app.get("/",response_class=HTMLResponse)
async def home(request:Request,tab:str="playout"):
    def load_tab():
        c=db()
        folders=c.execute("SELECT * FROM folders ORDER BY name").fetchall()
        media=[]; sched=[]; asrun=[]
        if tab=="scheduler":
            sched=c.execute("""SELECT s.*,m.title,m.duration,m.audio_json,m.subs_json
                               FROM schedule s JOIN media m ON m.id=s.media_id
                               ORDER BY s.start_at LIMIT 20""").fetchall()
            media=c.execute("""SELECT * FROM media
                               WHERE enabled=1 AND category IN ('Commercial','Promo')
                               ORDER BY title LIMIT 500""").fetchall()
        elif tab=="playout":
            sched=c.execute("""SELECT s.*,m.title,m.duration,m.audio_json,m.subs_json
                               FROM schedule s JOIN media m ON m.id=s.media_id
                               ORDER BY s.start_at LIMIT 20""").fetchall()
            asrun=c.execute("SELECT * FROM asrun ORDER BY id DESC LIMIT 20").fetchall()
        elif tab=="ads":
            media=c.execute("""SELECT * FROM media
                               WHERE enabled=1 AND category IN ('Commercial','Promo')
                               ORDER BY title LIMIT 500""").fetchall()
        c.close()
        return folders,media,sched,asrun

    folders,media,sched,asrun=await asyncio.to_thread(load_tab)
    current_event = await asyncio.to_thread(find_current_scheduled_event) if tab=="playout" else None

    # Estado del reproductor VLC (sin OBS). La lectura es ligera y cacheada
    # por el propio motor; nunca bloqueamos el servidor con libvlc.
    try:
        vstatus=get_engine().snapshot().get("player") or {}
    except Exception as e:
        vstatus={"available":False,"error":str(e),"state":"idle","has_input":False,
                 "position_ms":0,"length_ms":0,"playing":False}
    obs={"connected":bool(vstatus.get("available")),"scenes":[],"error":vstatus.get("error")}
    vlc_status=dict(vstatus)
    vlc_status["source"]="VLC"
    vlc_status["vlc_info"]=vlc_info()

    return templates.TemplateResponse(
        request=request,name="index.html",
        context={"tab":tab,"folders":folders,"media":media,"schedules":sched,
                 "asrun":asrun,"current_event":current_event,"state":STATE,"cfg":CFG,"obs":obs,
                 "vstatus":vlc_status,
                 "tmdb":tmdb_stats(),
                 "today":datetime.now().strftime("%Y-%m-%d"),
                 "month":datetime.now().strftime("%Y-%m")}
    )

@app.post("/folder/add")
async def folder_add(path:str=Form(...),name:str=Form(""),category:str=Form("Movie"),recursive:int=Form(1)):
 p=str(Path(path).expanduser());name=name or Path(p).name or p;c=db();c.execute("INSERT OR IGNORE INTO folders(path,name,category,recursive) VALUES(?,?,?,?)",(p,name,category,recursive));c.commit();fid=c.execute("SELECT id FROM folders WHERE path=?",(p,)).fetchone()["id"];c.close()
 asyncio.create_task(asyncio.to_thread(scan_folder,fid));return RedirectResponse("/?tab=scanner",303)

@app.post("/scan/{fid}")
async def scan(fid:int):
 asyncio.create_task(asyncio.to_thread(scan_folder,fid));return RedirectResponse("/?tab=scanner",303)

@app.get("/api/obs")
async def obs_api(refresh:int=0):
    # OBS ya no se usa: endpoint conservado para no romper lecturas antiguas.
    return {"connected":False,"scenes":[],"error":"OBS deshabilitado: el playout usa VLC como reproductor."}

@app.get("/api/tmdb")
async def tmdb_api_status():
    return tmdb_stats()

@app.post("/tmdb/config")
async def tmdb_config_save(enabled:int=Form(0),auto_enrich:int=Form(0),api_key:str=Form(""),language:str=Form("es-MX"),region:str=Form("MX")):
    cfg=CFG["tmdb"]
    cfg.update({"enabled":bool(enabled),"auto_enrich":bool(auto_enrich),"language":language.strip() or "es-MX","region":region.strip() or "MX"})
    if api_key.strip():
        cfg["api_key"]=api_key.strip()
    save_cfg()
    return RedirectResponse("/?tab=tmdb",303)

@app.post("/tmdb/run")
async def tmdb_run():
    if not CFG.get("tmdb",{}).get("api_key"):
        return RedirectResponse("/?tab=tmdb&error=key",303)
    CFG["tmdb"]["enabled"]=True
    CFG["tmdb"]["auto_enrich"]=False
    save_cfg()
    TMDB_RUN_EVENT.set()
    return RedirectResponse("/?tab=tmdb&run=started",303)

@app.post("/tmdb/enrich/{mid}")
async def tmdb_enrich_one(mid:int):
    if not CFG.get("tmdb",{}).get("api_key"):
        return JSONResponse({"ok":False,"error":"Configura la API Key de TMDB en el panel."},400)
    result=await asyncio.to_thread(tmdb_enrich_media,mid)
    return {"ok":result.get("status")=="found","result":result}

def tmdb_reset_database():
    """Clear TMDB matches/cache only; keep the media library and schedule intact."""
    TMDB_RUN_EVENT.clear()
    with TMDB_LOCK:
        c=db()
        c.execute("DELETE FROM tmdb_cache")
        c.commit()
        c.close()
        root=BASE/"cache"/"tmdb"
        # Delete all TMDB downloaded assets, then recreate poster/metadata dirs.
        if root.exists():
            for child in root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child,ignore_errors=True)
                else:
                    try: child.unlink()
                    except Exception: pass
        (root/"posters").mkdir(parents=True,exist_ok=True)
        (root/"metadata").mkdir(parents=True,exist_ok=True)
    TMDB_STATS.update({"running":False,"processed":0,"found":0,"not_found":0,
                       "errors":0,"pending":0,"last_title":"","last_error":""})
    return True

@app.post("/tmdb/reset")
async def tmdb_reset():
    if not CFG.get("tmdb",{}).get("api_key"):
        return RedirectResponse("/?tab=tmdb&error=key",303)
    await asyncio.to_thread(tmdb_reset_database)
    CFG["tmdb"]["enabled"]=True
    CFG["tmdb"]["auto_enrich"]=False
    CFG["tmdb"]["last_error"]=""
    save_cfg()
    TMDB_RUN_EVENT.set()
    return RedirectResponse("/?tab=tmdb&run=reset",303)

@app.post("/tmdb/retry-errors")
async def tmdb_retry_errors():
    c=db()
    c.execute("UPDATE tmdb_cache SET status='pending',error='' WHERE status='error'")
    c.commit(); c.close()
    TMDB_RUN_EVENT.set()
    return RedirectResponse("/?tab=tmdb",303)

@app.post("/config/ads")
async def config_ads(enabled:int=Form(0),interval_minutes:int=Form(60),break_seconds:int=Form(180),min_ads:int=Form(1),max_ads:int=Form(4),category:str=Form("Commercial"),avoid_repeat:int=Form(1)):
 CFG["auto_ads"].update({"enabled":bool(enabled),"interval_minutes":interval_minutes,"break_seconds":break_seconds,"min_ads":min_ads,"max_ads":max_ads,"category":category,"avoid_repeat":bool(avoid_repeat)});save_cfg();return RedirectResponse("/?tab=ads",303)

def _run_generation(kind, value, mode, category, avoid_repeat):
    GENERATION_STATE.update(running=True,type=kind,month=value if kind=="month" else "",start=value if kind=="week" else "",message="Generando...",result=None,error=None)
    try:
        if kind=="month":
            result=generate_month_weeks(value,mode,category,avoid_repeat)
        else:
            result=generate_week(value,mode,category,avoid_repeat,1,value[:7])
        GENERATION_STATE.update(running=False,message="Generación completada",result=result,error=None)
    except Exception as e:
        GENERATION_STATE.update(running=False,message="Error durante la generación",error=str(e))
        STATE["last_error"]=str(e)

@app.get("/api/generation")
async def generation_status():
    return JSONResponse(dict(GENERATION_STATE))

@app.post("/generate-month")
async def gen(month:str=Form(...),mode:str=Form("random"),category:str=Form("Movie"),avoid_repeat:int=Form(1)):
    global GENERATION_TASK
    if GENERATION_STATE.get("running"):
        return RedirectResponse("/?tab=scheduler&generation=busy",303)
    GENERATION_TASK=asyncio.create_task(asyncio.to_thread(_run_generation,"month",month,mode,category,bool(avoid_repeat)))
    return RedirectResponse("/?tab=scheduler&generation=started",303)

@app.post("/generate-week")
async def gen_week(start_date:str=Form(...),mode:str=Form("random"),category:str=Form("Movie"),avoid_repeat:int=Form(1)):
    global GENERATION_TASK
    if GENERATION_STATE.get("running"):
        return RedirectResponse("/?tab=scheduler&generation=busy",303)
    GENERATION_TASK=asyncio.create_task(asyncio.to_thread(_run_generation,"week",start_date,mode,category,bool(avoid_repeat)))
    return RedirectResponse("/?tab=scheduler&generation=started",303)

@app.post("/schedule/delete-week")
async def delete_week(start_date:str=Form(...)):
    try:
        st=date.fromisoformat(start_date)
        en=st+timedelta(days=7)
        c=db()
        c.execute(
            "DELETE FROM schedule WHERE source='AUTO_WEEKLY' AND status!='playing' AND day_key>=? AND day_key<?",
            (st.isoformat(),en.isoformat())
        )
        c.commit()
        c.close()
    except Exception as e:
        STATE["last_error"]=str(e)
    return RedirectResponse("/?tab=scheduler",303)

@app.post("/schedule/clear-month")
async def clear_month(month:str=Form(...)):
    try:
        y,m=map(int,month.split("-"))
        st=date(y,m,1)
        en=date(y+1,1,1) if m==12 else date(y,m+1,1)
        c=db()
        c.execute("DELETE FROM schedule WHERE status!='playing' AND day_key>=? AND day_key<?",(st.isoformat(),en.isoformat()))
        c.commit(); c.close()
    except Exception as e:
        STATE["last_error"]=str(e)
    return RedirectResponse("/?tab=scheduler",303)

@app.get("/api/playout")
async def playout_api():
    now=datetime.now()
    cur=await asyncio.to_thread(find_current_scheduled_event,now)
    nxt=await asyncio.to_thread(find_next_scheduled_event,now)
    upcoming=await asyncio.to_thread(find_upcoming_scheduled_events,now,5)
    def row_json(r):
        if not r:return None
        return {"id":r["id"],"title":r["title"],"start_at":r["start_at"],"end_at":r["end_at"],"duration":float(r["duration"] or 0),"kind":r["kind"],"audio_index":int(r["audio_index"] or 0),"subtitle_index":int(r["subtitle_index"] if r["subtitle_index"] is not None else -1),"audio_json":r["audio_json"] or "[]","subs_json":r["subs_json"] or "[]"}
    vstatus=await asyncio.to_thread(verify_vlc_output)
    STATE["vlc_ready"]=bool(vstatus.get("ready"))
    STATE["vlc_error"]=vstatus.get("error")
    try:
        esnap=get_engine().snapshot()
    except Exception:
        esnap={"mode":STATE.get("mode"),"ad_break":bool(STATE.get("ad_break")),
               "player":{"available":False,"state":"idle","error":""}}
    return {"now":now.strftime("%Y-%m-%dT%H:%M:%S"),"current":row_json(cur),"next":row_json(nxt),
            "upcoming":[row_json(x) for x in upcoming],
            "obs_connected":bool(STATE.get("obs_connected")),
            "mode":esnap.get("mode"),"ad_break":bool(esnap.get("ad_break")),
            "interrupted_title":esnap.get("interrupted_title"),
            "vlc":{"enabled":bool(CFG.get("vlc",{}).get("enabled",True)),"source":vlc_source_name(),
                   "scene":None,"ready":bool(vstatus.get("ready")),"connected":bool(vstatus.get("connected")),
                   "kind":vstatus.get("kind","libvlc"),"linked":True,
                   "playlist":int(vstatus.get("playlist",0) or 0),
                   "state":vstatus.get("state"),"error":vstatus.get("error"),
                   "player":esnap.get("player")}}

@app.post("/take/{mid}")
async def take(mid:int,audio_index:int=Form(0),subtitle_index:int=Form(-1)):
 c=db();r=c.execute("SELECT * FROM media WHERE id=?",(mid,)).fetchone();c.close()
 if not r:return JSONResponse({"ok":False},404)
 d=dict(r)
 res=await asyncio.to_thread(get_engine().take, d, int(audio_index), int(subtitle_index))
 if res.get("ok"):
     return RedirectResponse("/?tab=playout",303)
 return JSONResponse({"ok":False,"error":res.get("error","No se pudo tomar el medio")},500)

@app.post("/schedule/add")
async def schedule_add(mid:int=Form(...), start_at:str=Form(...), audio_index:int=Form(0), subtitle_index:int=Form(-1), kind:str=Form("PROGRAM")):
    c=db()
    m=c.execute("SELECT * FROM media WHERE id=?",(mid,)).fetchone()
    if not m:
        c.close()
        return JSONResponse({"ok":False,"error":"Medio no encontrado"},404)
    try:
        st=datetime.fromisoformat(start_at)
    except Exception:
        c.close()
        return JSONResponse({"ok":False,"error":"Fecha/hora inválida"},400)
    en=st+timedelta(seconds=float(m["duration"] or 0))
    c.execute("""INSERT INTO schedule(media_id,start_at,end_at,audio_index,subtitle_index,kind,status,source,day_key,generated_run)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (mid,st.strftime("%Y-%m-%dT%H:%M:%S"),en.strftime("%Y-%m-%dT%H:%M:%S"),
               audio_index,subtitle_index,kind,"scheduled","MANUAL",st.date().isoformat(),""))
    c.commit();c.close()
    return RedirectResponse("/?tab=scheduler",303)

@app.post("/schedule/clear-manual")
async def schedule_clear_manual():
    c=db();c.execute("DELETE FROM schedule WHERE source='MANUAL'");c.commit();c.close()
    return RedirectResponse("/?tab=scheduler",303)

@app.post("/config/channel")
async def config_channel_v92(name:str=Form("MOVIES HD"),scene:str=Form(""),source:str=Form("")):
    CFG["channel"]={"name":name,"scene":scene,"source":source}
    save_cfg()
    return RedirectResponse("/?tab=settings",303)

@app.post("/media/update")
async def media_update(mid:int=Form(...), title:str=Form(...), category:str=Form("Movie"), audio_default:int=Form(0), subtitle_default:int=Form(-1)):
    c=db()
    c.execute("UPDATE media SET title=?,category=? WHERE id=?",(title,category,mid))
    # Apply default audio/subtitle to existing manual/auto schedule rows for this media only.
    c.execute("UPDATE schedule SET audio_index=?,subtitle_index=? WHERE media_id=? AND status='scheduled'",(audio_default,subtitle_default,mid))
    c.commit();c.close()
    return RedirectResponse("/?tab=library",303)

@app.post("/media/delete/{mid}")
async def media_delete(mid:int):
    c=db()
    c.execute("DELETE FROM schedule WHERE media_id=? AND status='scheduled'",(mid,))
    c.execute("DELETE FROM playlist WHERE media_id=?",(mid,))
    c.execute("DELETE FROM media WHERE id=?",(mid,))
    c.commit();c.close()
    return RedirectResponse("/?tab=library",303)

def live_reload_tracks(row, audio_index, subtitle_index):
    """Cambio de audio/subtítulos al aire, sin recargar la película (VLC)."""
    row=dict(row)
    result=get_engine().reload_tracks(dict(row), int(audio_index), int(subtitle_index))
    if result.get("ok"):
        current=STATE.get("current") or {}
        STATE["current"]={
            **current,
            "audio_index":int(audio_index),
            "subtitle_index":int(subtitle_index),
            "position_ms":int(result.get("cursor",0) or 0),
        }
    return result

@app.post("/api/vlc/sync-playout")
async def vlc_sync_playout():
    try:
        res=await asyncio.to_thread(get_engine().seek_to_scheduler)
        if not res.get("ok"):
            return JSONResponse({"ok":False,
                                 "error":res.get("error","No se pudo sincronizar VLC"),
                                 "title":res.get("title",""),"target_ms":res.get("target_ms",0),
                                 "actual_ms":res.get("actual_ms",0)},500)
        return {"ok":True,"title":res.get("title"),"target_ms":res.get("target_ms",0),
                "actual_ms":res.get("actual_ms",0),"cursor_seconds":res.get("cursor_seconds",0)}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/api/vlc/load-current")
async def vlc_load_current():
    try:
        current=await asyncio.to_thread(find_current_scheduled_event)
        if not current:
            return JSONResponse({"ok":False,"error":"No hay película programada al aire"},404)
        result=await asyncio.to_thread(
            play_row,dict(current),0
        )
        return {"ok":True,"title":current["title"],"schedule_id":current["id"],"result":result}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.get("/api/vlc/sync-status")
async def vlc_sync_status():
    try:
        res=await asyncio.to_thread(get_engine().sync_status)
        return res
    except Exception as e:
        return {"ok":False,"error":str(e)}

@app.get("/api/vlc/status")
async def vlc_status():
    result=await asyncio.to_thread(verify_vlc_output)
    STATE["vlc_ready"]=bool(result.get("ready"))
    STATE["vlc_error"]=result.get("error")
    return result

@app.post("/api/vlc/setup")
async def vlc_setup():
    try:
        result=await asyncio.to_thread(ensure_vlc_player)
        if result.get("ok"):
            result["status"]=await asyncio.to_thread(verify_vlc_output)
        return result
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/api/vlc/action")
async def vlc_action(action:str=Form(...)):
    # play/pause/stop/restart/next/previous -> motor VLC (sin OBS)
    if action not in {"play","pause","stop","restart","next","previous"}:
        return JSONResponse({"ok":False,"error":"Acción VLC no válida"},400)
    try:
        res=await asyncio.to_thread(_vlc_action, action)
        if not res.get("ok"):
            code=500 if action in ("next","previous") else 409
            return JSONResponse({"ok":False,"error":res.get("error","Error VLC"),
                                 "action":action},code)
        return {"ok":True,"action":action,
                "scheduler_id":res.get("scheduler_id"),"title":res.get("title")}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/config/vlc")
async def config_vlc(channel_name:str=Form(""), fullscreen:int=Form(1), volume:int=Form(100),
                     network_caching:int=Form(300), lib_dir:str=Form(""), vlc_path:str=Form(""),
                     audio_language:str=Form("es,en,spa"), sub_language:str=Form("es,en,spa")):
    CFG.setdefault("vlc",{})
    V=CFG["vlc"]
    if channel_name.strip():
        CFG.setdefault("channel",{})["name"]=channel_name.strip()
    V["fullscreen"]=bool(fullscreen)
    V["volume"]=max(0,min(200,int(volume)))
    V["network_caching"]=max(50,int(network_caching))
    if lib_dir.strip():
        V["lib_dir"]=lib_dir.strip()
    if vlc_path.strip():
        V["path"]=vlc_path.strip()
    V["audio_language"]=(audio_language.strip() or "es,en,spa")
    V["sub_language"]=(sub_language.strip() or "es,en,spa")
    save_cfg()
    try:
        get_player().set_volume(V["volume"])
    except Exception:
        pass
    return RedirectResponse("/?tab=settings",303)

@app.post("/api/vlc/start")
async def vlc_start():
    try:
        res=await asyncio.to_thread(ensure_vlc_player)
        return {"ok":bool(res.get("ok")),"error":res.get("error"),"source":"VLC"}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/api/vlc/stop")
async def vlc_stop():
    try:
        res=await asyncio.to_thread(get_player().stop)
        return {"ok":bool(res.get("ok")),"error":res.get("error")}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/api/ads/cut-now")
async def ads_cut_now():
    res=await asyncio.to_thread(get_engine().ad_cut_now)
    if res.get("ok"):
        return res
    return JSONResponse({"ok":False,"error":res.get("error","No se pudo cortar")},409)

@app.post("/api/ads/skip")
async def ads_skip():
    res=await asyncio.to_thread(get_engine().ad_skip)
    if res.get("ok"):
        return res
    return JSONResponse({"ok":False,"error":res.get("error","No hay tanda en el aire")},409)

@app.get("/api/vlc/info")
async def vlc_info_api():
    return vlc_info()

@app.post("/schedule/update")
async def schedule_update(sid:int=Form(...), start_at:str=Form(...), audio_index:int=Form(0), subtitle_index:int=Form(-1), kind:str=Form("PROGRAM")):
    c=db()
    r=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                   FROM schedule s JOIN media m ON m.id=s.media_id WHERE s.id=?""",(sid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"ok":False,"error":"Evento no encontrado"},404)
    try:
        st=datetime.fromisoformat(start_at)
    except:
        c.close(); return JSONResponse({"ok":False,"error":"Fecha/hora inválida"},400)

    en=st+timedelta(seconds=float(r["duration"] or 0))
    now=datetime.now()
    was_current=(r["start_at"] <= now.strftime("%Y-%m-%dT%H:%M:%S") < r["end_at"])
    # Save the scheduling change first.
    c.execute("""UPDATE schedule SET start_at=?,end_at=?,audio_index=?,subtitle_index=?,kind=?,day_key=? WHERE id=?""",
              (st.strftime("%Y-%m-%dT%H:%M:%S"),en.strftime("%Y-%m-%dT%H:%M:%S"),
               int(audio_index),int(subtitle_index),kind,st.date().isoformat(),sid))
    c.commit()
    r2=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                    FROM schedule s JOIN media m ON m.id=s.media_id WHERE s.id=?""",(sid,)).fetchone()
    c.close()

    # Only touch OBS when the ON-AIR event actually changes its selected tracks.
    # Saving a schedule entry for a future movie must never reload the current movie.
    tracks_changed = (
        int(r["audio_index"] or 0) != int(audio_index) or
        int(r["subtitle_index"] if r["subtitle_index"] is not None else -1) != int(subtitle_index)
    )
    if was_current and r2 and tracks_changed:
        try:
            await asyncio.to_thread(live_reload_tracks,dict(r2),int(audio_index),int(subtitle_index))
            return RedirectResponse("/?tab=playout&live=1",303)
        except Exception as e:
            STATE["last_error"]=f"Cambio en vivo: {e}"
            return RedirectResponse("/?tab=scheduler&error=live",303)

    return RedirectResponse("/?tab=scheduler",303)

# ============================================================
# TVPlayout OBS Scene Manager
# Crea/repara las escenas estándar sin duplicar Multimedia.
# ============================================================
@app.get("/api/schedule")
async def schedule_api(page:int=1, per_page:int=20, q:str="", day:str=""):
    page=max(1,page); per_page=min(50,max(10,per_page)); offset=(page-1)*per_page
    def read():
        c=db(); where=["1=1"]; args=[]
        if q.strip(): where.append("m.title LIKE ?"); args.append(f"%{q.strip()}%")
        if day.strip(): where.append("substr(s.start_at,1,10)=?"); args.append(day.strip())
        where_sql=" AND ".join(where)
        total=c.execute(f"SELECT COUNT(*) FROM schedule s JOIN media m ON m.id=s.media_id WHERE {where_sql}",args).fetchone()[0]
        rows=c.execute(f"""SELECT s.*,m.title,m.duration,m.audio_json,m.subs_json FROM schedule s JOIN media m ON m.id=s.media_id
            WHERE {where_sql} ORDER BY s.start_at,s.id LIMIT ? OFFSET ?""",args+[per_page,offset]).fetchall(); c.close(); return total,rows
    total,rows=await asyncio.to_thread(read)
    return {"items":[dict(r) for r in rows],"count":total,"page":page,"per_page":per_page,"pages":max(1,(total+per_page-1)//per_page)}

@app.get("/api/tmdb/search")
async def tmdb_search_api(q:str=""):
    q=str(q or "").strip()
    if not q:
        return {"results":[]}
    try:
        data=tmdb_api("/search/movie", {"query":q,"region":tmdb_config().get("region","MX"),"include_adult":"false"})
        return {"results":(data.get("results") or [])[:20]}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.get("/api/tmdb/schedule-assets")
async def tmdb_schedule_assets(ids:str=""):
    vals=[]
    for x in str(ids or "").split(","):
        try: vals.append(int(x))
        except Exception: pass
    if not vals:
        return {"items":[]}
    marks=",".join("?" for _ in vals)
    c=db()
    rows=c.execute(f"""SELECT s.id AS schedule_id,s.media_id,t.tmdb_id,t.tmdb_title,t.poster_local
                       FROM schedule s LEFT JOIN tmdb_cache t ON t.media_id=s.media_id
                       WHERE s.id IN ({marks})""", tuple(vals)).fetchall()
    c.close()
    out=[]
    for r in rows:
        d=dict(r)
        d["poster_url"]=("/tmdb-cache/posters/"+Path(r["poster_local"]).name) if r["poster_local"] else ""
        out.append(d)
    return {"items":out}

@app.post("/api/tmdb/select")
async def tmdb_select_api(request:Request):
    data=await request.json()
    schedule_id=int(data.get("schedule_id",0)); tid=int(data.get("tmdb_id",0))
    if not schedule_id or not tid:
        return JSONResponse({"ok":False,"error":"Faltan schedule_id o tmdb_id"},400)
    c=db(); row=c.execute("SELECT media_id FROM schedule WHERE id=?",(schedule_id,)).fetchone(); c.close()
    if not row:
        return JSONResponse({"ok":False,"error":"Evento no encontrado"},404)
    media_id=int(row["media_id"])
    try:
        movie=tmdb_api(f"/movie/{tid}",{})
        root=BASE/"cache"/"tmdb"
        posters=root/"posters"; metadata=root/"metadata"
        posters.mkdir(parents=True,exist_ok=True); metadata.mkdir(parents=True,exist_ok=True)
        sid=str(tid)
        # Manual selection also downloads ONLY the selected movie poster.
        poster_local=tmdb_image_download(movie.get("poster_path"),posters/(sid+".jpg"),"w500") if movie.get("poster_path") else ""
        rd=str(movie.get("release_date") or "")
        year=int(rd[:4]) if rd[:4].isdigit() else None
        now=datetime.now().isoformat(timespec="seconds")
        c=db(); c.execute("""INSERT INTO tmdb_cache(media_id,tmdb_id,tmdb_title,tmdb_original_title,tmdb_year,poster_path,poster_local,logo_path,logo_local,backdrop_path,backdrop_local,status,error,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(media_id) DO UPDATE SET tmdb_id=excluded.tmdb_id,tmdb_title=excluded.tmdb_title,tmdb_original_title=excluded.tmdb_original_title,tmdb_year=excluded.tmdb_year,poster_path=excluded.poster_path,poster_local=excluded.poster_local,logo_path='',logo_local='',backdrop_path='',backdrop_local='',status='found',error='',updated_at=excluded.updated_at""",
          (media_id,tid,movie.get("title") or "",movie.get("original_title") or "",year,movie.get("poster_path") or "",poster_local,"","","","","found","",now))
        c.commit(); c.close()
        meta={"media_id":media_id,"tmdb_id":tid,"title":movie.get("title") or "","original_title":movie.get("original_title") or "","year":year,"poster_local":poster_local,"updated_at":now}
        (metadata/(str(media_id)+".json")).write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
        return {"ok":True,"tmdb_id":tid,"title":movie.get("title") or "","poster_url":("/tmdb-cache/posters/"+Path(poster_local).name) if poster_local else ""}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.get("/api/media/{mid}")
async def media_detail(mid:int):
    c=db(); r=c.execute("SELECT id,title,path,duration,width,height,category,audio_json,subs_json FROM media WHERE id=?",(mid,)).fetchone(); c.close()
    return dict(r) if r else JSONResponse({"ok":False,"error":"Medio no encontrado"},404)

@app.post("/api/schedule/update")
async def schedule_update_ajax(request:Request):
    data=await request.json(); sid=int(data.get("sid")); start_at=str(data.get("start_at")); audio_index=int(data.get("audio_index",0)); subtitle_index=int(data.get("subtitle_index",-1)); kind=str(data.get("kind","PROGRAM"))
    c=db(); r=c.execute("SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json FROM schedule s JOIN media m ON m.id=s.media_id WHERE s.id=?",(sid,)).fetchone()
    if not r: c.close(); return JSONResponse({"ok":False,"error":"Evento no encontrado"},404)
    st=datetime.fromisoformat(start_at); en=st+timedelta(seconds=float(r["duration"] or 0)); now=datetime.now()
    was_current=(r["start_at"]<=now.strftime("%Y-%m-%dT%H:%M:%S")<r["end_at"]); old_a=int(r["audio_index"] or 0); old_s=int(r["subtitle_index"] if r["subtitle_index"] is not None else -1)
    changed=(old_a!=audio_index or old_s!=subtitle_index)
    c.execute("UPDATE schedule SET start_at=?,end_at=?,audio_index=?,subtitle_index=?,kind=?,day_key=? WHERE id=?",(st.strftime("%Y-%m-%dT%H:%M:%S"),en.strftime("%Y-%m-%dT%H:%M:%S"),audio_index,subtitle_index,kind,st.date().isoformat(),sid)); c.commit(); r2=c.execute("SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json FROM schedule s JOIN media m ON m.id=s.media_id WHERE s.id=?",(sid,)).fetchone(); c.close()
    if was_current and changed:
        try: result=await asyncio.to_thread(live_reload_tracks,dict(r2),audio_index,subtitle_index)
        except Exception as e: return JSONResponse({"ok":False,"error":str(e),"saved":True},500)
        return {"ok":True,"saved":True,"live":True,"result":result}
    return {"ok":True,"saved":True,"live":False}

@app.post("/schedule/delete/{sid}")
async def schedule_delete_v10(sid:int):
    c=db();c.execute("DELETE FROM schedule WHERE id=?",(sid,));c.commit();c.close()
    return RedirectResponse("/?tab=scheduler",303)

@app.post("/schedule/add-ad")
async def schedule_add_ad(mid:int=Form(...), start_at:str=Form(...), audio_index:int=Form(0), subtitle_index:int=Form(-1)):
    c=db();m=c.execute("SELECT * FROM media WHERE id=?",(mid,)).fetchone()
    if not m:
        c.close();return JSONResponse({"ok":False,"error":"Anuncio no encontrado"},404)
    try:st=datetime.fromisoformat(start_at)
    except: c.close();return JSONResponse({"ok":False,"error":"Fecha/hora inválida"},400)
    en=st+timedelta(seconds=float(m["duration"] or 0))
    c.execute("""INSERT INTO schedule(media_id,start_at,end_at,audio_index,subtitle_index,kind,status,source,day_key,generated_run)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (mid,st.strftime("%Y-%m-%dT%H:%M:%S"),en.strftime("%Y-%m-%dT%H:%M:%S"),
               audio_index,subtitle_index,"COMMERCIAL","scheduled","MANUAL",st.date().isoformat(),""))
    c.commit();c.close()
    return RedirectResponse("/?tab=scheduler",303)

@app.get("/api/library")
async def library_api(page:int=1, per_page:int=50, q:str=""):
    page=max(1,page); per_page=min(50,max(10,per_page)); offset=(page-1)*per_page
    def read_page():
        c=db()
        if q.strip():
            like=f"%{q.strip()}%"
            total=c.execute("SELECT COUNT(*) FROM media WHERE enabled=1 AND title LIKE ?",(like,)).fetchone()[0]
            rows=c.execute("""SELECT m.id,m.title,m.path,m.duration,m.width,m.height,m.category,m.audio_json,m.subs_json,
                                     t.tmdb_id,t.tmdb_title,t.poster_local,t.status AS tmdb_status
                              FROM media m LEFT JOIN tmdb_cache t ON t.media_id=m.id
                              WHERE m.enabled=1 AND m.title LIKE ?
                              ORDER BY title LIMIT ? OFFSET ?""",(like,per_page,offset)).fetchall()
        else:
            total=c.execute("SELECT COUNT(*) FROM media WHERE enabled=1").fetchone()[0]
            rows=c.execute("""SELECT m.id,m.title,m.path,m.duration,m.width,m.height,m.category,m.audio_json,m.subs_json,
                                     t.tmdb_id,t.tmdb_title,t.poster_local,t.status AS tmdb_status
                              FROM media m LEFT JOIN tmdb_cache t ON t.media_id=m.id
                              WHERE m.enabled=1
                              ORDER BY m.title LIMIT ? OFFSET ?""",(per_page,offset)).fetchall()
        c.close()
        return total,rows
    total,rows=await asyncio.to_thread(read_page)
    return {"items":[dict(r) for r in rows],"count":total,"page":page,
            "per_page":per_page,"pages":max(1,(total+per_page-1)//per_page),
            "scanner":STATE["scanner"],"errors":STATE["scanner"]["errors"]}

@app.get("/api/status")
async def status():
    try:
        plinfo=await asyncio.to_thread(get_engine().player_status)
    except Exception as e:
        plinfo={"available":False,"error":str(e),"state":"idle","has_input":False,
                "position_ms":0,"length_ms":0,"playing":False}
    return {
        "ok":True,
        "state":STATE,
        "obs":{"connected":bool(STATE.get("obs_connected")),"scenes":[],"error":None},
        "vlc":plinfo,
        "ffmpeg":bins()[0],
        "ffprobe":bins()[1],
        "server_time":datetime.now().isoformat()
    }

if __name__=="__main__":
 import uvicorn;uvicorn.run(app,host=CFG["host"],port=CFG["port"])
