"""
TVPlayout 16 - Ads Engine
Motor independiente de tandas comerciales.

REGLA:
- No habla con OBS.
- No habla con VLC.
- No genera HLS/M3U8.
- Solo calcula y gestiona eventos COMMERCIAL/AUTO_ADS en SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import random
import sqlite3
from typing import Any


@dataclass
class AdsConfig:
    enabled: bool = False
    interval_minutes: int = 60
    min_ads: int = 1
    max_ads: int = 4
    category: str = "Commercial"
    avoid_repeat: bool = True
    min_program_tail_seconds: int = 90


class AdsEngine:
    def __init__(self, db_path, config: AdsConfig | None = None):
        self.db_path = str(db_path)
        self.config = config or AdsConfig()

    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def set_config(self, **values):
        for key in (
            "enabled", "interval_minutes", "min_ads", "max_ads",
            "category", "avoid_repeat", "min_program_tail_seconds"
        ):
            if key in values:
                setattr(self.config, key, values[key])

        self.config.interval_minutes = max(1, int(self.config.interval_minutes))
        self.config.min_ads = max(1, int(self.config.min_ads))
        self.config.max_ads = max(
            self.config.min_ads, int(self.config.max_ads)
        )
        self.config.min_program_tail_seconds = max(
            0, int(self.config.min_program_tail_seconds)
        )

    def library(self):
        c = self._db()
        try:
            if self.config.category in ("Commercial", "Promo"):
                rows = c.execute(
                    """SELECT id,title,path,duration,category,audio_json,subs_json
                       FROM media
                       WHERE enabled=1 AND duration>0 AND category=?
                       ORDER BY title""",
                    (self.config.category,)
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT id,title,path,duration,category,audio_json,subs_json
                       FROM media
                       WHERE enabled=1 AND duration>0
                         AND category IN ('Commercial','Promo')
                       ORDER BY title"""
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def select_ads(self, pool, count, used=None, rng=None):
        pool = [dict(x) for x in pool if float(x.get("duration") or 0) > 0]
        if not pool:
            return []

        used = set(used or ())
        rng = rng or random.Random()

        fresh = [x for x in pool if int(x["id"]) not in used]
        rng.shuffle(fresh)

        result = fresh[:count]

        # Si no hay suficientes anuncios diferentes, se reutilizan
        # después de agotar los no usados.
        if len(result) < count:
            rest = pool[:]
            rng.shuffle(rest)
            while len(result) < count:
                result.append(rest[len(result) % len(rest)])

        return result[:count]

    def build_break(self, pool, used=None, seed=None):
        if not pool:
            return {"ads": [], "seconds": 0.0}

        rng = random.Random(seed)
        count = rng.randint(
            int(self.config.min_ads),
            int(self.config.max_ads)
        )

        selected = self.select_ads(
            pool, count, used=used,
            rng=rng
        )

        return {
            "ads": selected,
            "seconds": round(
                sum(float(x.get("duration") or 0) for x in selected), 3
            )
        }

    def remove_generated(self, include_played=False):
        c = self._db()
        try:
            if include_played:
                cur = c.execute(
                    "DELETE FROM schedule WHERE source='AUTO_ADS'"
                )
            else:
                cur = c.execute(
                    """DELETE FROM schedule
                       WHERE source='AUTO_ADS'
                         AND status NOT IN ('playing')"""
                )
            c.commit()
            return cur.rowcount
        finally:
            c.close()

    def preview(self, start_at, end_at):
        """
        Devuelve una previsualización sin modificar SQLite.
        Solo coloca tandas entre PROGRAM existentes.
        """
        st = datetime.fromisoformat(str(start_at))
        en = datetime.fromisoformat(str(end_at))

        c = self._db()
        try:
            rows = c.execute(
                """SELECT s.*,m.title,m.duration,m.path,m.category
                   FROM schedule s
                   JOIN media m ON m.id=s.media_id
                   WHERE s.start_at>=? AND s.start_at<?
                     AND s.kind='PROGRAM'
                     AND s.status!='cancelled'
                   ORDER BY s.start_at,s.id""",
                (
                    st.strftime("%Y-%m-%dT%H:%M:%S"),
                    en.strftime("%Y-%m-%dT%H:%M:%S")
                )
            ).fetchall()
            programs = [dict(r) for r in rows]
        finally:
            c.close()

        pool = self.library()
        result = []
        used = set()

        if not pool:
            return {
                "ok": False,
                "error": "No hay comerciales habilitados.",
                "breaks": []
            }

        interval = timedelta(minutes=self.config.interval_minutes)

        for program in programs:
            pstart = datetime.fromisoformat(program["start_at"])
            pend = datetime.fromisoformat(program["end_at"])
            cursor = pstart + interval

            while cursor < pend:
                remaining = (pend - cursor).total_seconds()

                if remaining <= self.config.min_program_tail_seconds:
                    break

                br = self.build_break(
                    pool,
                    used=used,
                    seed=f"{program['id']}:{cursor.isoformat()}"
                )

                if not br["ads"] or br["seconds"] >= remaining:
                    break

                ad_start = cursor
                for ad in br["ads"]:
                    ad_end = ad_start + timedelta(
                        seconds=float(ad["duration"])
                    )
                    result.append({
                        "program_id": program["id"],
                        "program_title": program["title"],
                        "media_id": ad["id"],
                        "title": ad["title"],
                        "start_at": ad_start.strftime("%Y-%m-%dT%H:%M:%S"),
                        "end_at": ad_end.strftime("%Y-%m-%dT%H:%M:%S"),
                        "duration": float(ad["duration"]),
                        "kind": "COMMERCIAL",
                        "source": "AUTO_ADS"
                    })
                    used.add(int(ad["id"]))
                    ad_start = ad_end

                cursor = cursor + interval + timedelta(
                    seconds=br["seconds"]
                )

        return {
            "ok": True,
            "breaks": result,
            "ads": len(result),
            "seconds": round(sum(x["duration"] for x in result), 3)
        }

    def insert_preview(self, preview):
        """
        Inserta exactamente los eventos producidos por preview().
        No modifica eventos que no sean AUTO_ADS.
        """
        if not preview.get("ok"):
            return preview

        c = self._db()
        inserted = 0
        try:
            for item in preview.get("breaks", []):
                exists = c.execute(
                    """SELECT 1 FROM schedule
                       WHERE source='AUTO_ADS'
                         AND media_id=?
                         AND start_at=?
                       LIMIT 1""",
                    (item["media_id"], item["start_at"])
                ).fetchone()

                if exists:
                    continue

                st = item["start_at"]
                en = item["end_at"]
                day = st[:10]

                c.execute(
                    """INSERT INTO schedule
                       (media_id,start_at,end_at,audio_index,subtitle_index,
                        kind,status,source,day_key,generated_run)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(item["media_id"]),
                        st, en, 0, -1,
                        "COMMERCIAL", "scheduled",
                        "AUTO_ADS", day,
                        "AUTO_ADS"
                    )
                )
                inserted += 1

            c.commit()
            return {
                "ok": True,
                "inserted": inserted,
                "ads": len(preview.get("breaks", []))
            }
        finally:
            c.close()

    def generate(self, start_at, end_at, replace=True):
        if not self.config.enabled:
            return {"ok": False, "error": "Auto Ads está desactivado."}

        if replace:
            self.remove_generated(False)

        preview = self.preview(start_at, end_at)
        if not preview.get("ok"):
            return preview

        inserted = self.insert_preview(preview)
        return {
            **preview,
            **inserted,
            "mode": "generated"
        }
