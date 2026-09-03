import asyncio, json, os, random, sqlite3, subprocess, shutil, threading, time, re, hashlib, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, date, time as dtime, timedelta
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import obsws_python as obs

BASE=Path(__file__).resolve().parent; CFG_PATH=BASE/"config.json"; CFG=json.loads(CFG_PATH.read_text(encoding="utf-8"))
CFG.setdefault("vlc", {})
CFG["vlc"].setdefault("enabled", True)
CFG["vlc"].setdefault("source", "PLAYOUT VLC")
CFG["channel"].setdefault("source", CFG["vlc"]["source"])
CFG["vlc"].setdefault("network_caching", 300)
CFG["vlc"].setdefault("loop", False)
CFG["vlc"].setdefault("shuffle", False)
CFG.setdefault("title_overlay", {})
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
OBS_LOCK=threading.Lock()
LIVE_RELOAD_LOCK=threading.Lock()
OBS_CLIENT=None
OBS_CLIENT_KEY=None
STATE={"obs_connected":False,"obs_last_ok":0,"obs_last_error":None,
    "obs_failures":0,
    "obs_last_check":0.0,"obs_scenes":[],
       "vlc_ready":False,"vlc_error":None,
       "scanner":{"running":False,"paused":False,"folder":"","found":0,"analyzed":0,"pending":0,"errors":[],"started":None,"finished":None},
       "current":None,"next":None,"upcoming":[],"obs_connected":False,"last_error":None,"logo_on":False,"ad_break":False,
       "mode":"IDLE","title_overlay_on":False,"title_overlay_key":"","title_overlay_text":""}
app=FastAPI(title="TVPlayout 15.8 PRO · VLC/OBS")
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
                            try:
                                update_title_overlay(cur)
                                update_title_assets(cur)
                            except Exception:
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

def wrap_broadcast_title(value, max_chars=20):
    """Wrap a movie title on word boundaries for an OBS text source."""
    title=clean_display_title(value)
    max_chars=max(8,int(max_chars or 20))
    words=title.split()
    if not words: return ""
    lines=[]; line=""
    for word in words:
        candidate=word if not line else line+" "+word
        if line and len(candidate)>max_chars:
            lines.append(line); line=word
        else:
            line=candidate
    if line: lines.append(line)
    return "\n".join(lines)

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

def _selected_external_subtitle(row, sub_idx):
    try:
        si=int(sub_idx)
    except Exception:
        si=-1
    if si < 0:
        return None
    subs=safe_fromjson(row.get("subs_json","[]") if hasattr(row,"get") else "[]")
    if 0 <= si < len(subs):
        item=dict(subs[si] or {})
        if item.get("external"):
            p=Path(str(item.get("path") or ""))
            if p.exists():
                return item
    # Refresh from disk so files added after the global scan are visible.
    found=external_subs(Path(str(row.get("path") or "")))
    if not found:
        return None
    wanted_lang=""
    if 0 <= si < len(subs):
        wanted_lang=str((subs[si] or {}).get("language") or "").lower()
    if wanted_lang:
        for item in found:
            if item.get("language")==wanted_lang:
                return item
    return found[0]

def _subtitle_utf8_path(src_path):
    """Return a cached UTF-8 copy for text subtitles; never alter the original."""
    p=Path(str(src_path))
    if not p.exists():
        raise FileNotFoundError(str(p))
    raw=p.read_bytes()
    # UTF-8 with/without BOM first; then common Windows subtitle encodings.
    text=None
    for enc in ("utf-8-sig","utf-8","cp1252","latin-1"):
        try:
            text=raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text=raw.decode("latin-1","replace")
    cache=BASE/"cache"/"subtitles"
    cache.mkdir(parents=True,exist_ok=True)
    key=hashlib.sha1((str(p.resolve())+"|"+str(p.stat().st_mtime_ns)+"|"+str(p.stat().st_size)).encode("utf-8","ignore")).hexdigest()[:20]
    out=cache/f"{p.stem}.{key}.utf8{p.suffix.lower()}"
    if not out.exists():
        out.write_text(text,encoding="utf-8",newline="\n")
    return str(out)


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

def obs_disconnect():
    global OBS_CLIENT, OBS_CLIENT_KEY
    c=OBS_CLIENT
    OBS_CLIENT=None
    OBS_CLIENT_KEY=None
    if c is not None:
        try:
            c.disconnect()
        except Exception:
            pass

def obs_client():
    global OBS_CLIENT, OBS_CLIENT_KEY
    o=CFG["obs"]
    if not o.get("password"):
        raise RuntimeError("Configura la contraseña de OBS WebSocket.")
    key=(str(o.get("host","127.0.0.1")), int(o.get("port",4455)), str(o.get("password","")))
    if OBS_CLIENT is not None and OBS_CLIENT_KEY == key:
        return OBS_CLIENT
    obs_disconnect()
    OBS_CLIENT=obs.ReqClient(host=key[0],port=key[1],password=key[2],timeout=3)
    OBS_CLIENT_KEY=key
    return OBS_CLIENT

def obs_info(refresh=False):
    """Stable OBS health check.

    Avoids flashing CONNECTED/DISCONNECTED on every polling request.
    A transient WebSocket exception is tolerated; only 3 consecutive
    failed checks mark OBS disconnected.
    """
    now=time.time()
    last_check=float(STATE.get("obs_last_check",0) or 0)
    last_ok=float(STATE.get("obs_last_ok",0) or 0)
    failures=int(STATE.get("obs_failures",0) or 0)

    # Normal UI polling: don't touch the WebSocket more than once every 5s.
    if not refresh and (now-last_check)<5:
        return {
            "connected":bool(STATE.get("obs_connected")),
            "scenes":STATE.get("obs_scenes",[]),
            "error":STATE.get("obs_last_error")
        }

    STATE["obs_last_check"]=now
    try:
        with OBS_LOCK:
            c=obs_client()
            c.get_version()

        STATE["obs_failures"]=0
        STATE["obs_connected"]=True
        STATE["obs_last_ok"]=now
        STATE["obs_last_error"]=None
        return {"connected":True,"scenes":STATE.get("obs_scenes",[]),"error":None}

    except Exception as e:
        failures+=1
        STATE["obs_failures"]=failures
        STATE["obs_last_error"]=str(e)

        # Keep the last known connection state during transient failures.
        # Only close/reconnect after 3 consecutive failed checks.
        if failures>=3:
            obs_disconnect()
            STATE["obs_connected"]=False
        return {
            "connected":bool(STATE.get("obs_connected")) if failures<3 else False,
            "scenes":STATE.get("obs_scenes",[]),
            "error":str(e)
        }


def obs_refresh_scenes():
    try:
        with OBS_LOCK:
            c=obs_client()
            resp=c.get_scene_list()
            scenes=getattr(resp,"scenes",[]) or []
            out=[]
            for sc in scenes:
                name=sc.get("sceneName") if isinstance(sc,dict) else getattr(sc,"sceneName","")
                if not name: continue
                sources=[]
                try:
                    items=c.get_scene_item_list(name)
                    items=getattr(items,"scene_items",[]) or []
                    for x in items:
                        if isinstance(x,dict):
                            sources.append(x.get("sourceName",""))
                        else:
                            sources.append(getattr(x,"sourceName",""))
                except Exception:
                    pass
                out.append({"scene":name,"sources":[x for x in sources if x]})
        STATE["obs_scenes"]=out
        STATE["obs_connected"]=True
        STATE["obs_failures"]=0
        STATE["obs_last_check"]=time.time()
        STATE["obs_last_ok"]=time.time()
        STATE["obs_last_error"]=None
        return out
    except Exception as e:
        STATE["obs_failures"]=int(STATE.get("obs_failures",0) or 0)+1
        STATE["obs_last_error"]=str(e)
        # Keep the last known scenes; don't flap the UI for a single failure.
        return STATE.get("obs_scenes",[])



def _lower_url(text):
    cfg=CFG.get("title_overlay", {})
    params=urllib.parse.urlencode({
        "id": int(cfg.get("template", 3) or 3),
        "title": str(text or ""),
        "color1": str(cfg.get("color1", "#FFFFFF") or "#FFFFFF"),
        "color2": str(cfg.get("color2", "#00A8FF") or "#00A8FF"),
        "max": int(cfg.get("wrap_chars", 20) or 20),
        "v": int(time.time()*1000),
    })
    return f"http://127.0.0.1:8088/overlay/lower?{params}"

def obs_set_text(scene, source, text):
    if not scene or not source: return False
    with OBS_LOCK:
        c=obs_client()
        mode=str(CFG.get("title_overlay", {}).get("mode", "browser") or "browser").lower()
        if mode == "browser":
            c.set_input_settings(source, {"url": _lower_url(text)}, False)
        else:
            c.set_input_settings(source, {"text": str(text or "")}, False)
    return True

def _apply_poster_transform(c, scene, source):
    cfg=CFG.get("title_overlay",{})
    if not scene or not source:return
    owner=_find_scene_item_owner(c,scene,source)
    if not owner:return
    owner_scene,item_id=owner
    w=max(1,int(cfg.get("poster_width",180) or 180)); h=max(1,int(cfg.get("poster_height",260) or 260))
    x=float(cfg.get("poster_x",80) or 80); y=float(cfg.get("poster_y",820) or 820)
    try:
        cur=c.get_scene_item_transform(owner_scene,item_id)
        tr=getattr(cur,"scene_item_transform",None) or getattr(cur,"transform",None) or {}
        if not isinstance(tr,dict): tr={}
    except Exception: tr={}
    # OBS transform accepts position, scale and bounds. We use bounds so the image
    # is constrained without modifying the underlying poster file.
    transform=dict(tr)
    transform.update({"positionX":x,"positionY":y,"boundsWidth":float(w),"boundsHeight":float(h),
                      "boundsType":"OBS_BOUNDS_STRETCH" if not cfg.get("poster_keep_ratio",True) else "OBS_BOUNDS_SCALE_INNER"})
    c.set_scene_item_transform(owner_scene,item_id,transform)

def update_poster_layout():
    cfg=CFG.get("title_overlay",{})
    if not cfg.get("poster_source") or not cfg.get("scene"):return
    try:
        with OBS_LOCK: _apply_poster_transform(obs_client(),cfg["scene"],cfg["poster_source"])
    except Exception as e: STATE["last_error"]=f"Layout poster OBS: {e}"

def update_title_assets(row):
    cfg=CFG.get("title_overlay",{})
    media_id=row.get("media_id",row.get("id"))
    if not media_id: return
    assets=tmdb_media_assets(media_id)
    try:
        if cfg.get("poster_source") and assets.get("poster_local"):
            with OBS_LOCK:
                c=obs_client(); c.set_input_settings(cfg["poster_source"],{"file":assets["poster_local"]},False); _apply_poster_transform(c,cfg.get("scene",""),cfg["poster_source"])
    except Exception as e:
        STATE["last_error"]=f"Imagen TMDB OBS: {e}"

def _title_overlay_set_visible(visible):
    cfg=CFG.get("title_overlay", {})
    scene,source=cfg.get("scene",""),cfg.get("source","")
    if not scene or not source: return
    if STATE.get("title_overlay_on") == bool(visible): return
    set_source_enabled(scene,source,bool(visible))
    if cfg.get("poster_source"): set_source_enabled("",cfg.get("poster_source"),bool(visible)) if False else None
    if cfg.get("poster_source") and cfg.get("scene"): set_source_enabled(cfg.get("scene"),cfg.get("poster_source"),bool(visible))
    STATE["title_overlay_on"]=bool(visible)

def update_title_overlay(row, force_visible=None):
    cfg=CFG.get("title_overlay", {})
    if not cfg.get("enabled"): return
    kind=str(row.get("kind") or "PROGRAM")
    media_id=row.get("media_id",row.get("id"))
    display=tmdb_display_title(media_id,row.get("title") or "") if media_id else (row.get("title") or "")
    text="" if (kind=="COMMERCIAL" and not cfg.get("show_during_ads")) else wrap_broadcast_title(display,cfg.get("wrap_chars",20))
    key=f"{row.get('schedule_id',row.get('id',''))}:{text}"
    try:
        if key != STATE.get("title_overlay_key"):
            obs_set_text(cfg.get("scene",""),cfg.get("source",""),text)
            STATE["title_overlay_key"]=key; STATE["title_overlay_text"]=text
            try: update_title_assets(row)
            except Exception: pass
        if force_visible is not None: _title_overlay_set_visible(force_visible)
    except Exception as e: STATE["last_error"]=f"Título OBS: {e}"

def tick_title_overlay(row, now):
    cfg=CFG.get("title_overlay", {})
    if not cfg.get("enabled") or not row: return
    kind=str(row.get("kind") or "PROGRAM")
    if kind=="COMMERCIAL" and not cfg.get("show_during_ads"):
        update_title_overlay(row,False); return
    try: elapsed=max(0.0,(now-datetime.fromisoformat(str(row.get("start_at")))).total_seconds())
    except Exception: elapsed=0.0
    interval=max(0,int(cfg.get("interval_minutes",15) or 0))*60
    show_seconds=max(1,int(cfg.get("show_seconds",8) or 8))
    update_title_overlay(row)
    visible=True if interval<=0 else ((elapsed % interval)<show_seconds)
    update_title_overlay(row,visible)

def _find_scene_item_owner(c, scene_name, source_name, seen=None):
    seen=seen or set()
    if not scene_name or scene_name in seen: return None
    seen.add(scene_name)
    try: items=getattr(c.get_scene_item_list(scene_name),"scene_items",[]) or []
    except Exception: items=[]
    for item in items:
        if isinstance(item,dict):
            src=item.get("sourceName",""); sid=item.get("sceneItemId"); kind=str(item.get("inputKind") or "").lower()
        else:
            src=getattr(item,"sourceName",""); sid=getattr(item,"sceneItemId",None); kind=str(getattr(item,"inputKind","") or "").lower()
        if src==source_name and sid is not None: return scene_name,sid
        if src and ("scene" in kind or kind in {"scene","group"}):
            found=_find_scene_item_owner(c,src,source_name,seen)
            if found:return found
    return None

def set_source_enabled(scene,source,enabled):
    if not source:return False
    try:
        with OBS_LOCK:
            c=obs_client(); owner=_find_scene_item_owner(c,scene,source)
            if not owner:
                STATE["last_error"]=f"Fuente OBS no encontrada: {source}"; return False
            owner_scene,item_id=owner
            c.set_scene_item_enabled(owner_scene,item_id,bool(enabled))
            return True
    except Exception as e:
        STATE["last_error"]=f"Visibilidad OBS: {e}"; return False







# [REMOVED] build_playback_variant - Legacy remuxing function removed (was for HLS fallback)
    tmp=dst.with_suffix('.tmp.mkv')
    cmd=[ff,'-y','-hide_banner','-loglevel','error','-i',str(src),'-map','0:v:0?']
    if aud: cmd += ['-map',f'0:a:{audio_idx}?']
    if sub and sub.get('external'):
        cmd += ['-i',str(sub['path']),'-map','1:0?']
    elif sub:
        cmd += ['-map',f"0:s:{int(sub.get('ordinal',0) or 0)}?"]
    cmd += ['-c:v','copy']
    if aud: cmd += ['-c:a','copy']
    if sub: cmd += ['-c:s','copy','-metadata:s:s:0','language='+str(sub.get('language') or 'und')]
    else: cmd += ['-sn']
    cmd += ['-map_metadata','0','-avoid_negative_ts','make_zero',str(tmp)]
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',errors='replace',timeout=900)
    if p.returncode:
        try: tmp.unlink(missing_ok=True)
        except: pass
        raise RuntimeError(p.stderr[-3000:] or 'FFmpeg no pudo preparar la pista seleccionada.')
    tmp.replace(dst)
    return str(dst)

# [REMOVED] remux - Legacy remuxing wrapper removed

# [REMOVED] src_row_subs - Legacy subtitle lookup for remuxing removed


def obs_media_status(source):
    try:
        with OBS_LOCK:
            c=obs_client()
            return c.send("GetMediaInputStatus", {"inputName":source}, raw=True)
    except Exception:
        with OBS_LOCK:
            obs_disconnect()
        raise

def obs_set_cursor(source, position_ms):
    try:
        with OBS_LOCK:
            c=obs_client()
            return c.send("SetMediaInputCursor", {"inputName":source,"mediaCursor":int(max(0,position_ms))}, raw=True)
    except Exception:
        with OBS_LOCK:
            obs_disconnect()
        raise

def obs_play(source):
    try:
        with OBS_LOCK:
            c=obs_client()
            return c.send("TriggerMediaInputAction",
                          {"inputName":source,
                           "mediaAction":"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"}, raw=True)
    except Exception:
        with OBS_LOCK:
            obs_disconnect()
        raise

def wait_and_seek(source, position_ms, tries=12):
    # OBS must be playing or paused before SetMediaInputCursor is accepted.
    last=None
    for _ in range(tries):
        try:
            obs_play(source)
            time.sleep(0.35)
            last=obs_set_cursor(source, position_ms)
            return True, last
        except Exception as e:
            last=str(e)
            time.sleep(0.5)
    return False, last

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

def mark_event_playing(event_id):
    c=db()
    c.execute("UPDATE schedule SET status='playing' WHERE id=?",(event_id,))
    c.commit();c.close()

def mark_event_played(event_id):
    c=db()
    c.execute("UPDATE schedule SET status='played' WHERE id=?",(event_id,))
    c.commit();c.close()






VLC_STATUS_CACHE={"ts":0.0,"result":None}


def verify_vlc_output(force=False):
    """Verify VLC/OBS status, but cache UI health checks to protect OBS WebSocket."""
    now=time.monotonic()
    if not force and VLC_STATUS_CACHE["result"] is not None and now-float(VLC_STATUS_CACHE["ts"]) < 5:
        return dict(VLC_STATUS_CACHE["result"])
    scene=str(CFG.get("channel",{}).get("scene") or "")
    source=vlc_source_name()
    result={"ready":False,"scene":scene,"source":source,
            "connected":False,"kind":"","linked":False,"playlist":0,"error":None}
    try:
        with OBS_LOCK:
            c=obs_client()
            c.get_version()
            result["connected"]=True

            # GetInputList gives us the actual OBS input kind.
            raw=c.send("GetInputList",{},raw=True)
            inputs=raw.get("inputs",[]) if isinstance(raw,dict) else []
            item=next((x for x in inputs if x.get("inputName")==source),None)
            if not item:
                result["error"]=f'La fuente "{source}" no existe en OBS.'
                return result
            result["kind"]=str(item.get("inputKind") or "")
            if result["kind"]!="vlc_source":
                result["error"]=f'La fuente "{source}" no es VLC Video Source (kind={result["kind"]}).'
                return result

            # Verify it is actually inside the selected Program scene.
            raw2=c.send("GetSceneItemList",{"sceneName":scene},raw=True)
            items=raw2.get("sceneItems",[]) if isinstance(raw2,dict) else []
            linked=any(x.get("sourceName")==source for x in items)
            result["linked"]=linked
            if not linked:
                result["error"]=f'La fuente "{source}" existe pero no está dentro de la escena "{scene}".'
                return result

            settings=c.get_input_settings(name=source)
            st=getattr(settings,"input_settings",{}) or {}
            pl=st.get("playlist",[]) or []
            result["playlist"]=len(pl)
            result["ready"]=True
            VLC_STATUS_CACHE.update({"ts":now,"result":dict(result)})
            return result
    except Exception as e:
        result["error"]=str(e)
        VLC_STATUS_CACHE.update({"ts":now,"result":dict(result)})
        return result

def ensure_vlc_obs_source():
    """Prepare/verify the user-selected OBS scene + VLC Video Source."""
    source=vlc_source_name()
    scene=str(CFG.get("channel",{}).get("scene") or "LIVE PROGRAM")
    result={"ok":False,"scene":scene,"source":source,"created_scene":False,
            "created_source":False,"linked":False,"error":None}
    try:
        with OBS_LOCK:
            c=obs_client()

            # Use raw OBS WebSocket requests to avoid obsws-python keyword
            # differences between installed versions.
            def req(request_type, request_data=None):
                try:
                    r=c.send(request_type, request_data or {}, raw=True)
                    return r if isinstance(r,dict) else {}
                except TypeError:
                    # Older wrapper may not accept raw=True.
                    r=c.send(request_type, request_data or {})
                    return getattr(r,"datain",{}) or getattr(r,"data",{}) or {}

            # Check scene.
            scenes=req("GetSceneList")
            scene_exists=any(x.get("sceneName")==scene for x in scenes.get("scenes",[]))
            if not scene_exists:
                req("CreateScene",{"sceneName":scene})
                result["created_scene"]=True

            # Check input.
            inputs=req("GetInputList")
            inp=next((x for x in inputs.get("inputs",[]) if x.get("inputName")==source),None)

            if not inp:
                req("CreateInput",{
                    "sceneName":scene,
                    "inputName":source,
                    "inputKind":"vlc_source",
                    "inputSettings":{
                        "playlist":[],
                        "loop":False,
                        "shuffle":False,
                        "playback_behavior":"always_play",
                        "network_caching":int(CFG.get("vlc",{}).get("network_caching",300)),
                        "track":1,
                        "subtitle_enable":False,
                        "subtitle":1
                    },
                    "sceneItemEnabled":True
                })
                result["created_source"]=True
            elif inp.get("inputKind")!="vlc_source":
                raise RuntimeError(
                    f'La fuente "{source}" ya existe pero no es VLC Video Source '
                    f'(inputKind={inp.get("inputKind")}).'
                )

            # Check/link source to selected scene.
            items=req("GetSceneItemList",{"sceneName":scene})
            linked=any(x.get("sourceName")==source for x in items.get("sceneItems",[]))
            if not linked:
                req("CreateSceneItem",{"sceneName":scene,"sourceName":source})
                result["linked"]=True

            # Make selected scene the program scene.
            try:
                req("SetCurrentProgramScene",{"sceneName":scene})
            except Exception:
                pass

        CFG["channel"]["scene"]=scene
        CFG["channel"]["source"]=source
        CFG.setdefault("vlc",{})["source"]=source
        save_cfg()

        status=verify_vlc_output()
        result["status"]=status
        result["ok"]=bool(status.get("ready") or status.get("connected"))
        if not result["ok"]:
            result["error"]=status.get("error") or "OBS no confirmó la salida VLC."
        STATE["vlc_ready"]=bool(status.get("ready"))
        STATE["vlc_error"]=status.get("error")
        return result
    except Exception as e:
        STATE["vlc_ready"]=False
        STATE["vlc_error"]=str(e)
        result["error"]=str(e)
        return result


def vlc_source_name():
    # The user chooses the actual OBS Program output source in Settings.
    # TVPlayout uses that source as the VLC engine.
    return str(CFG.get("channel", {}).get("source")
               or CFG.get("vlc", {}).get("source")
               or "PLAYOUT VLC")

def _vlc_apply_settings(path, audio_index=0, subtitle_index=-1, restart=True):
    """Load one original media file into OBS's VLC source."""
    setup=ensure_vlc_obs_source()
    if not setup.get("ok"):
        raise RuntimeError("No se pudo preparar VLC Video Source en OBS: "+str(setup.get("error") or "error desconocido"))

    source=vlc_source_name()
    if not path:
        raise RuntimeError("La película no tiene ruta.")
    p=str(Path(path).resolve())

    # OBS's VLC source expects playlist entries with a 'value' path.
    # Include selected/hidden fields because OBS can preserve them from the UI.
    playlist=[{"value":p,"hidden":False,"selected":True}]
    settings={
        "playlist":playlist,
        "loop":False,
        "shuffle":False,
        "playback_behavior":"always_play",
        "network_caching":int(CFG.get("vlc",{}).get("network_caching",300)),
        "track":int(audio_index)+1,
        "subtitle_enable":int(subtitle_index)>=0,
        "subtitle":int(subtitle_index)+1 if int(subtitle_index)>=0 else 1,
    }

    with OBS_LOCK:
        c=obs_client()
        c.set_input_settings(source,settings,False)

        # Verify OBS actually accepted the playlist.
        check=c.get_input_settings(name=source)
        accepted=getattr(check,"input_settings",{}) or {}
        accepted_playlist=accepted.get("playlist",[])
        if not accepted_playlist:
            raise RuntimeError("OBS no aceptó la playlist VLC. La fuente PLAYOUT VLC quedó vacía.")

        # Make sure the source is the active program scene.
        try:
            c.set_current_program_scene(scene_name=str(CFG.get("channel",{}).get("scene") or "LIVE PROGRAM"))
        except Exception:
            pass

        if restart:
            try:
                c.trigger_media_input_action(
                    source,"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
                )
            except Exception:
                # A fresh playlist normally starts automatically with always_play.
                pass

    return {"ok":True,"source":source,"path":p,
            "audio_track":settings["track"],
            "subtitle_track":settings["subtitle"],
            "subtitle_enabled":settings["subtitle_enable"],
            "playlist":accepted_playlist}



def _scheduler_live_cursor(row):
    """Return milliseconds elapsed since the Scheduler event start."""
    try:
        start=row.get("start_at") or row.get("start")
        if not start:
            return 0
        dt=datetime.fromisoformat(str(start).replace("Z",""))
        elapsed=max(0,(datetime.now()-dt).total_seconds()*1000.0)
        duration=float(row.get("duration") or 0)*1000.0
        if duration>0:
            elapsed=min(elapsed,max(0,duration-500))
        return int(elapsed)
    except Exception:
        return 0

def _vlc_media_status(source):
    """Read actual OBS VLC media duration/cursor."""
    with OBS_LOCK:
        c=obs_client()
        try:
            r=c.send("GetMediaInputStatus",{"inputName":source},raw=True)
            if isinstance(r,dict):
                return r
        except Exception:
            pass
        try:
            r=c.get_media_input_status(input_name=source)
            return {
                "mediaState":getattr(r,"media_state",getattr(r,"mediaState","")),
                "mediaDuration":getattr(r,"media_duration",getattr(r,"mediaDuration",0)),
                "mediaCursor":getattr(r,"media_cursor",getattr(r,"mediaCursor",0)),
            }
        except Exception as e:
            return {"error":str(e)}
    return {}

def _vlc_seek_cursor(source,cursor_ms):
    """Seek OBS VLC Video Source. OBS expects mediaCursor in INTEGER ms."""
    target=max(0,int(cursor_ms or 0))
    last_error=None

    # Wait briefly for VLC to expose a duration after a new playlist is loaded.
    import time as _time
    for _ in range(30):
        try:
            st=_vlc_media_status(source)
            if int(st.get("mediaDuration",0) or 0)>0:
                break
        except Exception:
            pass
        _time.sleep(0.10)

    with OBS_LOCK:
        c=obs_client()
        try:
            r=c.send("SetMediaInputCursor",{
                "inputName":source,
                "mediaCursor":target
            },raw=True)
            # OBS acknowledges the request even if a media source is not ready;
            # verify the actual cursor after the request.
        except Exception as e:
            last_error=str(e)

        if last_error:
            try:
                c.set_media_input_cursor(
                    input_name=source,
                    media_cursor=target
                )
                last_error=None
            except Exception as e:
                last_error=str(e)

    # Verify actual cursor, allowing a small tolerance.
    for _ in range(20):
        _time.sleep(0.10)
        st=_vlc_media_status(source)
        actual=int(st.get("mediaCursor",0) or 0)
        if abs(actual-target)<=1500:
            return {"ok":True,"target_ms":target,"actual_ms":actual,"status":st}
    st=_vlc_media_status(source)
    actual=int(st.get("mediaCursor",0) or 0)
    return {"ok":False,"target_ms":target,"actual_ms":actual,
            "error":last_error or "OBS VLC no alcanzó la posición solicitada.",
            "status":st}


def _vlc_action(action):
    source=vlc_source_name()

    # If Play/Restart is requested and the source is empty, load the scheduled
    # current event instead of merely sending PLAY to an empty VLC playlist.
    if action in {
        "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
        "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
    }:
        try:
            with OBS_LOCK:
                c=obs_client()
                st=c.get_input_settings(name=source)
                settings=getattr(st,"input_settings",{}) or {}
            if not settings.get("playlist"):
                current=find_current_scheduled_event()
                if current:
                    return _vlc_apply_settings(
                        current["path"],
                        int(current.get("audio_index") or 0),
                        int(current.get("subtitle_index") if current.get("subtitle_index") is not None else -1),
                        restart=True
                    )
        except Exception:
            pass

    with OBS_LOCK:
        c=obs_client()
        c.trigger_media_input_action(source,action)
    return {"ok":True,"source":source,"action":action}


def play_row(row, resume_ms=0):
    """Put the scheduled movie on air using OBS VLC Video Source and the original MKV."""
    row=dict(row)
    try:
        row["title"]=tmdb_display_title(
            row.get("media_id",row.get("id")), row.get("title") or ""
        )
    except Exception:
        row["title"]=clean_display_title(row.get("title") or "")
    if not CFG.get("vlc",{}).get("enabled",True):
        raise RuntimeError("Motor VLC deshabilitado.")
    ai=int(row.get("audio_index") or 0)
    si=int(row.get("subtitle_index") if row.get("subtitle_index") is not None else -1)
    scheduler_cursor=_scheduler_live_cursor(row)
    target_cursor=int(resume_ms or 0) if int(resume_ms or 0)>0 else scheduler_cursor
    _vlc_apply_settings(row.get("path"),ai,si,restart=True)
    seek_result={"ok":True,"target_ms":0,"actual_ms":0}
    if target_cursor>0:
        seek_result=_vlc_seek_cursor(vlc_source_name(),target_cursor)
        if not seek_result.get("ok"):
            raise RuntimeError(
                "VLC no pudo sincronizar con el Scheduler: "
                f"objetivo={target_cursor}ms, actual={seek_result.get('actual_ms',0)}ms; "
                f"{seek_result.get('error','')}"
            )
    kind=row["kind"]
    STATE.update(current={
        "schedule_id":row.get("schedule_id",row.get("id")),
        "media_id":row.get("media_id",row.get("id")),
        "title":row["title"],
        "duration":float(row["duration"] or 0),
        "audio_index":ai,
        "subtitle_index":si,
        "kind":kind,
        "position_ms":int(target_cursor),
        "vlc_source":vlc_source_name()
    },mode=kind,last_error=None)
    update_title_overlay(row)
    mark_event_playing(row.get("schedule_id",row.get("id")))

    if kind=="COMMERCIAL" and CFG["logo"].get("show_during_ads"):
        set_source_enabled(CFG["logo"].get("scene",""),CFG["logo"].get("source",""),True)
        STATE["logo_on"]=True; STATE["ad_break"]=True
    elif STATE["logo_on"]:
        set_source_enabled(CFG["logo"].get("scene",""),CFG["logo"].get("source",""),False)
        STATE["logo_on"]=False; STATE["ad_break"]=False

    c=db()
    c.execute("""INSERT INTO asrun
                 (event_time,media_id,title,kind,audio_index,subtitle_index,duration,status)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               row.get("media_id",row.get("id")),row["title"],kind,ai,si,
               row["duration"],"PLAYED"))
    c.commit(); c.close()


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

def next_event():
    now=datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    c=db()
    row=c.execute(
        """SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
           FROM schedule s JOIN media m ON m.id=s.media_id
           WHERE s.status='scheduled'
           ORDER BY s.start_at,s.id LIMIT 1"""
    ).fetchone()
    c.close()
    return row

def next_two_events():
    c=db()
    rows=c.execute(
        """SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
           FROM schedule s JOIN media m ON m.id=s.media_id
           WHERE s.status='scheduled'
           ORDER BY s.start_at,s.id LIMIT 2"""
    ).fetchall()
    c.close()
    return rows

# ============================================================
# TVPlayout 13.4 - INFO/CLEAN Auto Transition Controller
# Uses EXISTING OBS scenes only. It never creates/renames scenes.
# ============================================================
INFO_SCENE_HINTS = (
    "INFO", "PROGRAM INFO", "LIVE PROGRAM INFO",
)
CLEAN_SCENE_HINTS = (
    "CLEAN", "PROGRAM", "LIVE PROGRAM",
)

INFO_TRANSITION_CFG = {
    "enabled": True,
    "delay_after_movie_start": 5,
    "show_seconds": 8,
    "transition_name": "",
    "commercial_disable": True,
}
INFO_TRANSITION_STATE = {
    "schedule_id": None,
    "mode": "clean",
    "info_until": 0.0,
    "movie_started_at": None,
    "last_transition": None,
}

def _scene_names_from_obs(client):
    try:
        r=client.get_scene_list()
        scenes=getattr(r,"scenes",None) or getattr(r,"scene_list",None) or []
        out=[]
        for x in scenes:
            if isinstance(x,dict):
                n=x.get("sceneName") or x.get("name")
            else:
                n=getattr(x,"sceneName",None) or getattr(x,"name",None)
            if n: out.append(str(n))
        return out
    except Exception:
        return []

def _pick_existing_scene(names, hints, exclude=None):
    exclude=exclude or set()
    # Exact/contains matching, preferring the most specific name.
    scored=[]
    for n in names:
        if n in exclude: continue
        u=n.upper()
        score=0
        for h in hints:
            if u==h: score=max(score,100)
            elif h in u: score=max(score,80)
        if score: scored.append((score,-len(n),n))
    scored.sort(reverse=True)
    return scored[0][2] if scored else ""

def _resolve_info_clean_scenes():
    client=_tvp_obs_client() if "_tvp_obs_client" in globals() else None
    if not client:
        try: client=obs_client()
        except Exception: return "", ""
    names=_scene_names_from_obs(client)
    if not names: return "", ""
    info=_pick_existing_scene(names, INFO_SCENE_HINTS)
    clean=_pick_existing_scene(names, CLEAN_SCENE_HINTS, {info} if info else set())
    return clean,info

def _obs_transition_to(scene_name):
    client=_tvp_obs_client() if "_tvp_obs_client" in globals() else None
    if not client or not scene_name: return False
    try:
        client.set_current_program_scene(scene_name=scene_name)
        return True
    except Exception:
        return False

def _obs_set_preview(scene_name):
    client=_tvp_obs_client() if "_tvp_obs_client" in globals() else None
    if not client or not scene_name: return False
    try:
        client.set_current_preview_scene(scene_name=scene_name)
        return True
    except Exception:
        return False

def _obs_do_transition(transition_name=""):
    client=_tvp_obs_client() if "_tvp_obs_client" in globals() else None
    if not client: return False
    try:
        if transition_name:
            try:
                client.set_current_scene_transition(transition_name=transition_name)
            except Exception:
                pass
        client.trigger_transition()
        return True
    except Exception:
        return False

def _set_program_scene_safely(scene_name):
    client=_tvp_obs_client() if "_tvp_obs_client" in globals() else None
    if not client or not scene_name: return False
    try:
        # If Studio Mode is active, use Preview + Transition.
        status=client.get_studio_mode_enabled()
        enabled=bool(getattr(status,"studio_mode_enabled",False))
        if enabled:
            client.set_current_preview_scene(scene_name=scene_name)
            _obs_do_transition(INFO_TRANSITION_CFG.get("transition_name",""))
        else:
            client.set_current_program_scene(scene_name=scene_name)
        return True
    except Exception:
        try:
            client.set_current_program_scene(scene_name=scene_name)
            return True
        except Exception:
            return False

def _info_auto_transition(row, now):
    if not INFO_TRANSITION_CFG["enabled"] or not row:
        return
    kind=str(row.get("kind") or "PROGRAM").upper()
    sid=row.get("schedule_id",row.get("id"))
    if sid != INFO_TRANSITION_STATE.get("schedule_id"):
        INFO_TRANSITION_STATE["schedule_id"]=sid
        INFO_TRANSITION_STATE["mode"]="clean"
        INFO_TRANSITION_STATE["info_until"]=0.0
        INFO_TRANSITION_STATE["movie_started_at"]=time.monotonic()
        INFO_TRANSITION_STATE["last_transition"]=None

    if kind=="COMMERCIAL" and INFO_TRANSITION_CFG["commercial_disable"]:
        if INFO_TRANSITION_STATE["mode"]!="clean":
            clean,info=_resolve_info_clean_scenes()
            if clean:
                _set_program_scene_safely(clean)
            INFO_TRANSITION_STATE["mode"]="clean"
        return

    if kind!="PROGRAM":
        return

    elapsed=time.monotonic()-float(INFO_TRANSITION_STATE.get("movie_started_at") or time.monotonic())
    delay=float(INFO_TRANSITION_CFG["delay_after_movie_start"])
    show=float(INFO_TRANSITION_CFG["show_seconds"])

    # Movie start: CLEAN -> INFO after delay.
    if INFO_TRANSITION_STATE["mode"]=="clean" and elapsed>=delay:
        clean,info=_resolve_info_clean_scenes()
        if info:
            if _set_program_scene_safely(info):
                INFO_TRANSITION_STATE["mode"]="info"
                INFO_TRANSITION_STATE["info_until"]=time.monotonic()+show
                INFO_TRANSITION_STATE["last_transition"]="to_info"
        return

    # INFO -> CLEAN after show duration.
    if INFO_TRANSITION_STATE["mode"]=="info" and time.monotonic()>=INFO_TRANSITION_STATE["info_until"]:
        clean,info=_resolve_info_clean_scenes()
        if clean:
            if _set_program_scene_safely(clean):
                INFO_TRANSITION_STATE["mode"]="clean"
                INFO_TRANSITION_STATE["last_transition"]="to_clean"


async def engine():
    last_event_id=None
    obs_was_ok=False
    obs_failures=0

    # The Scheduler clock is authoritative. On application startup, if a
    # movie is already in progress, load it directly at its scheduled cursor.
    try:
        current=find_current_scheduled_event()
        if current:
            start_dt=datetime.fromisoformat(current["start_at"])
            offset_ms=max(0,int((datetime.now()-start_dt).total_seconds()*1000))
            if offset_ms < int(float(current["duration"])*1000):
                play_row(current,resume_ms=offset_ms)
                last_event_id=current["id"]
    except Exception as e:
        STATE["last_error"]=f"Inicio del playout: {e}"

    while True:
        try:
            now=datetime.now()
            current=find_current_scheduled_event(now)

            if current:
                if last_event_id != current["id"]:
                    elapsed_ms=max(0,int((now-datetime.fromisoformat(current["start_at"])).total_seconds()*1000))
                    duration_ms=int(float(current["duration"] or 0)*1000)
                    resume_ms=elapsed_ms if 0 < elapsed_ms < duration_ms else 0
                    play_row(current,resume_ms=resume_ms)
                    last_event_id=current["id"]
                    obs_was_ok=True
                    obs_failures=0

                # Lightweight OBS health check is cached by obs_info().
                try:
                    obs_info_live=obs_info()
                    STATE["obs_connected"]=bool(obs_info_live.get("connected",True))
                    tick_title_overlay(dict(current), now)
                except Exception as e:
                    obs_failures += 1
                    STATE["obs_last_error"]=str(e)
                    if obs_failures >= 3:
                        STATE["obs_connected"]=False
                    else:
                        STATE["obs_connected"]=True
            else:
                last_event_id=None
                obs_was_ok=False
                obs_failures=0

            nxt=find_next_scheduled_event(now)
            upcoming=find_upcoming_scheduled_events(now,5)
            STATE["next"]={
                "title":nxt["title"],"start_at":nxt["start_at"],
                "end_at":nxt["end_at"],"duration":nxt["duration"],"kind":nxt["kind"]
            } if nxt else None
            STATE["upcoming"]=[{
                "id":r["id"],"title":r["title"],"start_at":r["start_at"],
                "end_at":r["end_at"],"duration":r["duration"],"kind":r["kind"]
            } for r in upcoming]

        except Exception as e:
            STATE["last_error"]=str(e)

        await asyncio.sleep(0.5)


async def vlc_startup_setup():
    for _ in range(20):
        try:
            if obs_info(refresh=True).get("connected"):
                await asyncio.to_thread(ensure_vlc_obs_source)
                current=await asyncio.to_thread(find_current_scheduled_event)
                if current:
                    try:
                        await asyncio.to_thread(play_row,dict(current),0)
                    except Exception as e:
                        STATE["vlc_error"]=str(e)
                return
        except Exception:
            pass
        await asyncio.sleep(1)

def disable_obsolete_program_sources():
    """Disable legacy program sources that are no longer used by TVPlayout.

    The source is only disabled, never deleted, so an existing OBS scene can be
    restored manually if needed. The current playout uses the configured VLC
    Video Source directly.
    """
    legacy_names={"Program live","PROGRAM LIVE","Program Live"}
    disabled=[]
    try:
        with OBS_LOCK:
            c=obs_client()
            scenes_resp=c.get_scene_list()
            scenes=getattr(scenes_resp,"scenes",[]) or []
            for sc in scenes:
                scene=sc.get("sceneName") if isinstance(sc,dict) else getattr(sc,"sceneName","")
                if not scene:
                    continue
                try:
                    items=c.get_scene_item_list(scene_name=scene)
                    raw=getattr(items,"scene_items",[]) or []
                except Exception:
                    raw=[]
                for item in raw:
                    if isinstance(item,dict):
                        name=item.get("sourceName") or item.get("source_name")
                        sid=item.get("sceneItemId") or item.get("scene_item_id")
                    else:
                        name=getattr(item,"sourceName",getattr(item,"source_name",None))
                        sid=getattr(item,"sceneItemId",getattr(item,"scene_item_id",None))
                    if name in legacy_names and sid is not None:
                        try:
                            c.set_scene_item_enabled(scene_name=scene,scene_item_id=int(sid),scene_item_enabled=False)
                            disabled.append(f"{scene}/{name}")
                        except Exception:
                            pass
    except Exception:
        pass
    return disabled


@app.on_event("startup")
async def startup():
 global TMDB_TASK
 init_db()
 normalize_existing_titles()
 try:
  disable_obsolete_program_sources()
 except Exception:
  pass
 # TMDB worker must be started at application startup. Without this task,
 # /tmdb/run only sets an Event that no coroutine ever consumes, so no
 # search/download can occur.
 if TMDB_TASK is None or TMDB_TASK.done():
  TMDB_TASK = asyncio.create_task(tmdb_worker())
 asyncio.create_task(engine())

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

    # WebSocket calls can be slow; only ask OBS on tabs that need it.
    obs={"connected":bool(STATE.get("obs_connected")),"scenes":STATE.get("obs_scenes",[]),"error":STATE.get("obs_last_error")}
    if tab in ("playout","settings","logo","tmdb"):
        try:
            obs=await asyncio.to_thread(obs_info,False)
            if tab in ("settings","logo","tmdb") and not obs.get("scenes"):
                await asyncio.to_thread(obs_refresh_scenes)
                obs=await asyncio.to_thread(obs_info,False)
        except Exception as e:
            obs={"connected":False,"scenes":STATE.get("obs_scenes",[]),"error":str(e)}

    return templates.TemplateResponse(
        request=request,name="index.html",
        context={"tab":tab,"folders":folders,"media":media,"schedules":sched,
                 "asrun":asrun,"current_event":current_event,"state":STATE,"cfg":CFG,"obs":obs,
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
    if refresh:
        scenes=await asyncio.to_thread(obs_refresh_scenes)
    else:
        scenes=STATE.get("obs_scenes",[])
    info=await asyncio.to_thread(obs_info, bool(refresh))
    return {"connected":info.get("connected",False),"scenes":scenes,"error":info.get("error")}

@app.get("/overlay/lower", response_class=HTMLResponse)
async def local_lower_overlay():
    return (BASE / "templates" / "lower.html").read_text(encoding="utf-8")

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

@app.post("/config/title-overlay")
async def config_title_overlay(scene:str=Form(""),source:str=Form(""),poster_source:str=Form(""),enabled:int=Form(0),show_during_ads:int=Form(0),interval_minutes:int=Form(15),show_seconds:int=Form(8),wrap_chars:int=Form(20),mode:str=Form("gdi"),template:int=Form(3),color1:str=Form("#FFFFFF"),color2:str=Form("#00A8FF"),poster_width:int=Form(180),poster_height:int=Form(260),poster_x:int=Form(80),poster_y:int=Form(820),poster_keep_ratio:int=Form(1),poster_crop:int=Form(0)):
    CFG["title_overlay"].update({"scene":scene,"source":source,"poster_source":poster_source,"enabled":bool(enabled),"show_during_ads":bool(show_during_ads),"interval_minutes":max(0,interval_minutes),"show_seconds":max(1,show_seconds),"wrap_chars":max(8,wrap_chars),"mode":mode if mode in {"browser","gdi"} else "gdi","template":max(1,min(5,template)),"color1":color1 if re.fullmatch(r"#[0-9A-Fa-f]{6}",color1 or "") else "#FFFFFF","color2":color2 if re.fullmatch(r"#[0-9A-Fa-f]{6}",color2 or "") else "#00A8FF","poster_width":max(1,poster_width),"poster_height":max(1,poster_height),"poster_x":poster_x,"poster_y":poster_y,"poster_keep_ratio":bool(poster_keep_ratio),"poster_crop":bool(poster_crop)})
    save_cfg()
    try: update_poster_layout()
    except Exception: pass
    if CFG["title_overlay"].get("enabled"):
        cur=find_current_scheduled_event()
        if cur:
            try: update_title_overlay(dict(cur))
            except Exception: pass
    return RedirectResponse("/?tab=settings",303)

@app.post("/config/logo")
async def config_logo(scene:str=Form(""),source:str=Form(""),enabled:int=Form(1)):
 CFG["logo"].update({"scene":scene,"source":source,"enabled":bool(enabled)});save_cfg();return RedirectResponse("/?tab=logo",303)

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
    return {"now":now.strftime("%Y-%m-%dT%H:%M:%S"),"current":row_json(cur),"next":row_json(nxt),"upcoming":[row_json(x) for x in upcoming],"obs_connected":bool(STATE.get("obs_connected")),"vlc":{"enabled":bool(CFG.get("vlc",{}).get("enabled",True)),"source":vlc_source_name(),"scene":CFG.get("channel",{}).get("scene",""),"ready":bool(vstatus.get("ready")),"connected":bool(vstatus.get("connected")),"kind":vstatus.get("kind",""),"linked":bool(vstatus.get("linked")),"playlist":int(vstatus.get("playlist",0) or 0),"error":vstatus.get("error")}}

@app.post("/take/{mid}")
async def take(mid:int,audio_index:int=Form(0),subtitle_index:int=Form(-1)):
 c=db();r=c.execute("SELECT *, 'TAKE' AS kind FROM media WHERE id=?",(mid,)).fetchone();c.close()
 if not r:return JSONResponse({"ok":False},404)
 d=dict(r);d["audio_index"]=audio_index;d["subtitle_index"]=subtitle_index
 try:play_row(d);return RedirectResponse("/?tab=playout",303)
 except Exception as e:return JSONResponse({"ok":False,"error":str(e)},500)

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


def current_cursor_for_source(source, fallback_ms=0):
    try:
        st=obs_media_status(source)
        data=st.get("responseData",st) if isinstance(st,dict) else {}
        cur=data.get("mediaCursor")
        if cur is not None:
            return max(0,int(cur))
    except Exception:
        pass
    return max(0,int(fallback_ms or 0))

def live_reload_tracks(row, audio_index, subtitle_index):
    """Apply audio/subtitle selection through the OBS VLC Video Source."""
    row=dict(row)
    current=STATE.get("current") or {}
    cursor=int(current.get("position_ms",0) or 0)
    result=_vlc_apply_settings(
        row.get("path"),int(audio_index),int(subtitle_index),restart=True
    )
    row["audio_index"]=int(audio_index)
    row["subtitle_index"]=int(subtitle_index)
    STATE["current"]={
        **current,
        "audio_index":int(audio_index),
        "subtitle_index":int(subtitle_index),
        "position_ms":cursor,
        "vlc_source":vlc_source_name()
    }
    return {"cursor":cursor,"reloaded":True,**result}


@app.post("/api/vlc/sync-playout")
async def vlc_sync_playout():
    try:
        current=await asyncio.to_thread(find_current_scheduled_event)
        if not current:
            return JSONResponse({"ok":False,"error":"No hay evento al aire"},404)
        target=await asyncio.to_thread(_scheduler_live_cursor,dict(current))
        result=await asyncio.to_thread(_vlc_seek_cursor,vlc_source_name(),target)
        if not result.get("ok"):
            return JSONResponse({
                "ok":False,
                "error":result.get("error","No se pudo sincronizar VLC"),
                "title":current["title"],
                "target_ms":target,
                "actual_ms":result.get("actual_ms",0)
            },500)
        STATE["current"]["position_ms"]=int(target)
        return {
            "ok":True,
            "title":current["title"],
            "target_ms":target,
            "actual_ms":result.get("actual_ms",0),
            "cursor_seconds":round(target/1000,2)
        }
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
        current=await asyncio.to_thread(find_current_scheduled_event)
        if not current:
            return {"ok":False,"error":"No hay evento al aire"}
        target=await asyncio.to_thread(_scheduler_live_cursor,dict(current))
        actual=await asyncio.to_thread(_vlc_media_status,vlc_source_name())
        return {"ok":True,"title":current["title"],"target_ms":target,
                "actual_ms":int(actual.get("mediaCursor",0) or 0),
                "duration_ms":int(actual.get("mediaDuration",0) or 0),
                "state":actual.get("mediaState","")}
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
        result=await asyncio.to_thread(ensure_vlc_obs_source)
        if result.get("ok"):
            result["status"]=await asyncio.to_thread(verify_vlc_output)
        return result
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)},500)

@app.post("/api/vlc/action")
async def vlc_action(action:str=Form(...)):
    # Play/Pause/Stop/Restart are true VLC controls.
    if action in {"play","pause","stop","restart"}:
        actions={
            "play":"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
            "pause":"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
            "stop":"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
            "restart":"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
        }
        try:
            return _vlc_action(actions[action])
        except Exception as e:
            return JSONResponse({"ok":False,"error":str(e)},500)

    # NEXT is owned by the Scheduler. This prevents VLC from escaping the
    # programmed order and guarantees that the database remains authoritative.
    if action=="next":
        try:
            now=datetime.now()
            cur=find_current_scheduled_event(now)
            nxt=find_next_scheduled_event(now)
            if cur:
                mark_event_played(cur["id"])
            if not nxt:
                return JSONResponse({"ok":False,"error":"No hay siguiente evento programado"},404)
            play_row(dict(nxt),resume_ms=0)
            return {"ok":True,"action":"next","scheduler_id":nxt["id"],"title":nxt["title"]}
        except Exception as e:
            return JSONResponse({"ok":False,"error":str(e)},500)

    if action=="previous":
        try:
            now=datetime.now()
            cur=find_current_scheduled_event(now)
            c=db()
            if cur:
                prev=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                                  FROM schedule s JOIN media m ON m.id=s.media_id
                                  WHERE s.start_at < ? AND s.status IN ('scheduled','played')
                                  ORDER BY s.start_at DESC LIMIT 1""",
                               (cur["start_at"],)).fetchone()
            else:
                prev=c.execute("""SELECT s.*,m.title,m.duration,m.path,m.audio_json,m.subs_json
                                  FROM schedule s JOIN media m ON m.id=s.media_id
                                  WHERE s.start_at < ? AND s.status IN ('scheduled','played')
                                  ORDER BY s.start_at DESC LIMIT 1""",
                               (now.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()
            c.close()
            if not prev:
                return JSONResponse({"ok":False,"error":"No hay evento anterior"},404)
            play_row(dict(prev),resume_ms=0)
            return {"ok":True,"action":"previous","scheduler_id":prev["id"],"title":prev["title"]}
        except Exception as e:
            return JSONResponse({"ok":False,"error":str(e)},500)

    return JSONResponse({"ok":False,"error":"Acción VLC no válida"},400)

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
TVP_SCENES = {
    "program": "TVP — PROGRAM",
    "info": "TVP — PROGRAM INFO",
    "commercial": "TVP — COMMERCIAL",
}
TVP_SOURCES = {
    "media": "Multimedia",
    "poster": "Poster TMDB",
    "title": "Título GDI+",
}

def _tvp_obs_client():
    try:
        return obs_client()
    except Exception:
        return None

def _tvp_source_exists(client, name):
    try:
        client.get_input_settings(name=name)
        return True
    except Exception:
        return False

def _tvp_scene_exists(client, scene):
    try:
        client.get_scene_item_list(scene_name=scene)
        return True
    except Exception:
        return False

def _tvp_ensure_scenes():
    client = _tvp_obs_client()
    if not client:
        return {"ok": False, "error": "OBS no conectado"}

    created = []
    linked = []
    for scene in TVP_SCENES.values():
        if not _tvp_scene_exists(client, scene):
            try:
                client.create_scene(scene_name=scene)
                created.append(scene)
            except Exception:
                pass

    available = {k: _tvp_source_exists(client, v) for k, v in TVP_SOURCES.items()}
    wanted = {
        TVP_SCENES["program"]: ["media"],
        TVP_SCENES["info"]: ["media", "poster", "title"],
        TVP_SCENES["commercial"]: ["media"],
    }

    for scene, keys in wanted.items():
        try:
            data = client.get_scene_item_list(scene_name=scene)
            items = getattr(data, "scene_items", []) or []
            existing = {getattr(x, "source_name", None) for x in items}
            for key in keys:
                name = TVP_SOURCES[key]
                if available.get(key) and name not in existing:
                    try:
                        client.create_scene_item(scene_name=scene, source_name=name)
                        linked.append(f"{scene}: {name}")
                    except Exception:
                        pass
        except Exception:
            pass

    return {"ok": True, "created": created, "linked": linked,
            "scenes": TVP_SCENES, "sources": available}

@app.get("/api/obs/scenes")
async def tvp_obs_scenes():
    return _tvp_ensure_scenes()

@app.post("/api/obs/scenes/ensure")
async def tvp_obs_scenes_ensure():
    return _tvp_ensure_scenes()

@app.post("/api/obs/scenes/set")
async def tvp_obs_scene_set(request: Request):
    data = await request.json()
    mode = str(data.get("mode") or "program").lower()
    scene = TVP_SCENES.get(mode)
    if not scene:
        return JSONResponse({"ok": False, "error": "modo inválido"}, 400)
    client = _tvp_obs_client()
    if not client:
        return JSONResponse({"ok": False, "error": "OBS no conectado"}, 503)
    try:
        if bool(data.get("preview", False)):
            client.set_current_preview_scene(scene_name=scene)
        else:
            client.set_current_program_scene(scene_name=scene)
        return {"ok": True, "scene": scene, "mode": mode}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)

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

@app.post("/config/obs-output")
async def config_obs_output(scene:str=Form(""),source:str=Form("")):
    CFG["channel"]["scene"]=scene
    CFG["channel"]["source"]=source
    CFG.setdefault("vlc",{})["source"]=source
    save_cfg()
    # If OBS is connected, immediately verify the newly selected output.
    try:
        result=await asyncio.to_thread(verify_vlc_output)
        STATE["vlc_ready"]=bool(result.get("ready"))
        STATE["vlc_error"]=result.get("error")
    except Exception as e:
        STATE["vlc_ready"]=False
        STATE["vlc_error"]=str(e)
    return RedirectResponse("/?tab=settings",303)

@app.post("/config/logo-v10")
async def config_logo_v10(scene:str=Form(""),source:str=Form(""),enabled:int=Form(0),show_during_ads:int=Form(1)):
    CFG["logo"].update({"scene":scene,"source":source,"enabled":bool(enabled),"show_during_ads":bool(show_during_ads)})
    save_cfg()
    return RedirectResponse("/?tab=logo",303)

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
    return {
        "ok":True,
        "state":STATE,
        "obs":obs_info(),
        "ffmpeg":bins()[0],
        "ffprobe":bins()[1],
        "server_time":datetime.now().isoformat()
    }

if __name__=="__main__":
 import uvicorn;uvicorn.run(app,host=CFG["host"],port=CFG["port"])


@app.get("/api/obs/info-transition")
async def obs_info_transition_status():
    return {"ok": True, "config": dict(INFO_TRANSITION_CFG),
            "state": dict(INFO_TRANSITION_STATE)}

@app.post("/api/obs/info-transition")
async def obs_info_transition_config(request: Request):
    data=await request.json()
    for k in ("enabled","commercial_disable"):
        if k in data: INFO_TRANSITION_CFG[k]=bool(data[k])
    for k in ("delay_after_movie_start","show_seconds"):
        if k in data:
            INFO_TRANSITION_CFG[k]=max(1,int(data[k]))
    if "transition_name" in data:
        INFO_TRANSITION_CFG["transition_name"]=str(data["transition_name"] or "")
    return {"ok": True, "config": dict(INFO_TRANSITION_CFG)}