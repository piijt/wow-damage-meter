"""SQLite history: every closed segment is saved in full (actors, spells, every hit, casts, deaths).

Segments are keyed by (log file, open time), so replaying a log on startup never stores duplicates.
"""
import json, os, queue, sqlite3, threading
from wowlog import Segment, Actor, Spell

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "combat.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY, log_file TEXT, opened REAL, start REAL, last REAL, duration REAL,
    name TEXT, label TEXT, forced INTEGER, me_key TEXT, me_name TEXT, UNIQUE(log_file, opened));
CREATE TABLE IF NOT EXISTS actors (
    id INTEGER PRIMARY KEY, segment_id INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    key TEXT, name TEXT, class TEXT, spec TEXT, hostile INTEGER, is_player INTEGER,
    dmg INTEGER, heal INTEGER, overheal INTEGER, taken INTEGER, deaths INTEGER);
CREATE TABLE IF NOT EXISTS spells (
    id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES actors(id) ON DELETE CASCADE,
    book TEXT, name TEXT, total INTEGER, hits INTEGER, crits INTEGER, ticks INTEGER, over INTEGER,
    absorbed INTEGER, n_sum INTEGER, n_min INTEGER, n_max INTEGER, c_sum INTEGER, c_min INTEGER, c_max INTEGER,
    misses TEXT, targets TEXT);
CREATE TABLE IF NOT EXISTS hits (
    spell_id INTEGER REFERENCES spells(id) ON DELETE CASCADE, t REAL, amount INTEGER, crit INTEGER,
    kind TEXT, over INTEGER, absorbed INTEGER, mitigated INTEGER, hp REAL, other TEXT);
CREATE TABLE IF NOT EXISTS casts (
    actor_id INTEGER REFERENCES actors(id) ON DELETE CASCADE, t REAL, spell TEXT, target TEXT, resource TEXT);
CREATE TABLE IF NOT EXISTS utility (
    actor_id INTEGER REFERENCES actors(id) ON DELETE CASCADE, type TEXT, t REAL, used TEXT, target TEXT, what TEXT);
CREATE TABLE IF NOT EXISTS deaths (
    segment_id INTEGER REFERENCES segments(id) ON DELETE CASCADE, t REAL, key TEXT, name TEXT,
    hostile INTEGER, killer TEXT, recap TEXT);
CREATE TABLE IF NOT EXISTS arena_matches (
    id INTEGER PRIMARY KEY, log_file TEXT, start REAL, end REAL, bracket TEXT, type TEXT, zone TEXT,
    me_key TEXT, me_name TEXT, result TEXT, rw INTEGER, rl INTEGER, rd INTEGER, duration REAL,
    rating1 INTEGER, rating2 INTEGER, rounds TEXT, UNIQUE(log_file, start));
CREATE INDEX IF NOT EXISTS ix_actors_seg ON actors(segment_id);
CREATE INDEX IF NOT EXISTS ix_spells_actor ON spells(actor_id);
CREATE INDEX IF NOT EXISTS ix_hits_spell ON hits(spell_id);
CREATE INDEX IF NOT EXISTS ix_casts_actor ON casts(actor_id);
"""


def connect():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    return db


class Store:
    """Writes closed segments on a background thread; reads use their own connection."""
    def __init__(self, state):
        self.state = state
        self.q = queue.Queue()
        self.saved = 0
        connect().close()
        threading.Thread(target=self._writer, daemon=True).start()
        state.on_close = self.save
        state.on_match = self.save_match

    def save(self, seg):
        """Called by the parser (under the state lock) when a segment closes."""
        s = self.state
        if not seg.start:
            return                                   # opened but nothing happened
        classes = {k: s.classes.get(k) for k in seg.actors}
        self.q.put(("segment", seg, s.log_file, seg.label(), classes, s.me, s.my_name))

    def save_match(self, m):
        self.q.put(("match", m, self.state.log_file))

    def flush(self):
        self.q.join()

    def _writer(self):
        db = connect()
        while True:
            item = self.q.get()
            try:
                if item[0] == "match":
                    self._write_match(db, *item[1:])
                else:
                    self._write(db, *item[1:])
            except Exception as e:                   # never kill the writer thread
                print("store error:", e)
            finally:
                self.q.task_done()

    def _write_match(self, db, m, log_file):
        with db:
            db.execute("INSERT OR IGNORE INTO arena_matches (log_file, start, end, bracket, type, zone, me_key, me_name,"
                       " result, rw, rl, rd, duration, rating1, rating2, rounds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (os.path.basename(log_file or ""), m["start"], m["end"], m["bracket"], m["type"], m["zone"],
                        m["me_key"], m["me_name"], m["result"], m["rw"], m["rl"], m["rd"], m["duration"],
                        m["ratings"][0], m["ratings"][1], json.dumps(m["rounds"])))

    def _write(self, db, seg, log_file, label, classes, me_key, me_name):
        with db:
            cur = db.execute("INSERT OR IGNORE INTO segments (log_file, opened, start, last, duration, name, label,"
                             " forced, me_key, me_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (os.path.basename(log_file or ""), seg.opened, seg.start, seg.last, seg.duration,
                              seg.name, label, int(seg.forced), me_key, me_name))
            if cur.rowcount == 0:
                return                               # already stored (log replay)
            sid = cur.lastrowid
            for a in list(seg.actors.values()):
                cls, spec = classes.get(a.key) or (None, None)
                aid = db.execute("INSERT INTO actors (segment_id, key, name, class, spec, hostile, is_player, dmg,"
                                 " heal, overheal, taken, deaths) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, a.key, a.name, cls, spec, int(a.hostile), int(a.is_player), a.dmg_total,
                                  a.heal_total, a.overheal, a.taken_total, a.deaths)).lastrowid
                for book in ("dmg", "heal", "taken", "ff"):
                    for sp in list(getattr(a, book).values()):
                        spid = db.execute(
                            "INSERT INTO spells (actor_id, book, name, total, hits, crits, ticks, over, absorbed,"
                            " n_sum, n_min, n_max, c_sum, c_min, c_max, misses, targets)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (aid, book, sp.name, sp.total, sp.hits, sp.crits, sp.ticks, sp.over, sp.absorbed,
                             sp.n_sum, sp.n_min, sp.n_max, sp.c_sum, sp.c_min, sp.c_max,
                             json.dumps(sp.misses), json.dumps(sp.targets))).lastrowid
                        db.executemany("INSERT INTO hits VALUES (?,?,?,?,?,?,?,?,?,?)",
                                       [(spid, t, amt, int(cr), k, o, ab, mi, hp, ot)
                                        for t, amt, cr, k, o, ab, mi, hp, ot in sp.hit_list])
                db.executemany("INSERT INTO casts VALUES (?,?,?,?,?)", [(aid, *c) for c in a.casts])
                db.executemany("INSERT INTO utility VALUES (?,?,?,?,?,?)",
                               [(aid, "interrupt", *x) for x in a.interrupts] +
                               [(aid, "dispel", *x) for x in a.dispels])
            db.executemany("INSERT INTO deaths VALUES (?,?,?,?,?,?,?)",
                           [(sid, d["t"], d["key"], d["name"], int(d["hostile"]), d["killer"],
                             json.dumps({"recap": d["recap"], "blow": d.get("blow", "")}))
                            for d in seg.deaths])
        self.saved += 1


# ---------------------------------------------------------------------- reading
def list_segments(search=""):
    """Summary rows for the history window, newest first."""
    db = connect()
    like = f"%{search}%"
    rows = db.execute("""
        SELECT s.id, s.opened, s.label, s.duration, s.forced,
               (SELECT COUNT(*) FROM actors a WHERE a.segment_id = s.id AND a.is_player) AS players,
               (SELECT a.dmg FROM actors a WHERE a.segment_id = s.id AND a.key = s.me_key) AS my_dmg,
               (SELECT a.name || '|' || a.dmg FROM actors a WHERE a.segment_id = s.id
                    ORDER BY a.dmg DESC LIMIT 1) AS top,
               (SELECT COUNT(*) FROM deaths d WHERE d.segment_id = s.id) AS deaths
        FROM segments s
        WHERE s.label LIKE ? OR EXISTS (SELECT 1 FROM actors a WHERE a.segment_id = s.id AND a.name LIKE ?)
        ORDER BY s.opened DESC""", (like, like)).fetchall()
    db.close()
    return rows


def delete_segment(sid):
    db = connect()
    with db:
        db.execute("DELETE FROM segments WHERE id = ?", (sid,))
    db.close()


def load_segment(sid):
    """Rebuild a full Segment (same objects the live meter uses) from the database."""
    db = connect()
    s = db.execute("SELECT opened, start, last, name, label, forced FROM segments WHERE id = ?", (sid,)).fetchone()
    if not s:
        return None
    opened, start, last, name, label, forced = s
    seg = Segment(name or label.split("  ", 1)[-1].rsplit("  (", 1)[0], opened, bool(forced))
    seg.start, seg.last, seg.closed, seg.history_id = start, last, True, sid
    for (aid, key, aname, cls, spec, hostile, _, dmg, heal, overheal, taken, deaths) in db.execute(
            "SELECT id, key, name, class, spec, hostile, is_player, dmg, heal, overheal, taken, deaths"
            " FROM actors WHERE segment_id = ?", (sid,)):
        a = seg.actors[key] = Actor(key, aname, bool(hostile))
        a.cls, a.spec = cls, spec
        a.dmg_total, a.heal_total, a.overheal, a.taken_total, a.deaths = dmg, heal, overheal, taken, deaths
        spells = {}
        for row in db.execute("SELECT id, book, name, total, hits, crits, ticks, over, absorbed, n_sum, n_min,"
                              " n_max, c_sum, c_min, c_max, misses, targets FROM spells WHERE actor_id = ?", (aid,)):
            sp = Spell(row[2])
            (sp.total, sp.hits, sp.crits, sp.ticks, sp.over, sp.absorbed,
             sp.n_sum, sp.n_min, sp.n_max, sp.c_sum, sp.c_min, sp.c_max) = row[3:15]
            sp.misses.update(json.loads(row[15])); sp.targets.update(json.loads(row[16]))
            getattr(a, row[1])[sp.name] = sp
            if row[1] == "ff":
                a.ff_total += sp.total
            spells[row[0]] = sp
        if spells:
            q = ",".join("?" * len(spells))
            for spid, t, amt, cr, k, o, ab, mi, hp, ot in db.execute(
                    f"SELECT * FROM hits WHERE spell_id IN ({q}) ORDER BY t", list(spells)):
                spells[spid].hit_list.append((t, amt, bool(cr), k, o, ab, mi, hp, ot))
        for book, buckets in ((a.dmg, a.buckets), (a.heal, a.hbuckets)):
            for sp in book.values():
                for h in sp.hit_list:
                    buckets[int(h[0])] += h[1]
        a.casts = [tuple(r) for r in db.execute("SELECT t, spell, target, resource FROM casts WHERE actor_id = ?"
                                                " ORDER BY t", (aid,))]
        for typ, t, used, tgt, what in db.execute("SELECT type, t, used, target, what FROM utility"
                                                  " WHERE actor_id = ? ORDER BY t", (aid,)):
            (a.interrupts if typ == "interrupt" else a.dispels).append((t, used, tgt, what))
    for t, key, dname, hostile, killer, recap in db.execute(
            "SELECT t, key, name, hostile, killer, recap FROM deaths WHERE segment_id = ? ORDER BY t", (sid,)):
        data = json.loads(recap)
        if isinstance(data, list):                   # saved before the killing blow was stored
            data = {"recap": data, "blow": ""}
        seg.deaths.append({"t": t, "key": key, "name": dname, "hostile": bool(hostile), "killer": killer,
                           "recap": [tuple(e) for e in data["recap"]], "blow": data["blow"]})
    db.close()
    return seg


# ---------------------------------------------------------------------- arena
def arena_characters():
    db = connect()
    rows = [r[0] for r in db.execute("SELECT me_name FROM arena_matches GROUP BY me_name ORDER BY MAX(start) DESC")]
    db.close()
    return rows


def list_matches(me_name=None, since=None):
    """Arena matches newest first, rounds decoded."""
    db = connect()
    q, args = "SELECT id, start, end, bracket, type, zone, me_name, result, rw, rl, rd, duration, rating1, rating2,"               " rounds FROM arena_matches WHERE 1=1", []
    if me_name:
        q += " AND me_name = ?"; args.append(me_name)
    if since:
        q += " AND start >= ?"; args.append(since)
    out = []
    for row in db.execute(q + " ORDER BY start DESC", args):
        keys = ("id", "start", "end", "bracket", "type", "zone", "me_name", "result", "rw", "rl", "rd", "duration",
                "rating1", "rating2")
        m = dict(zip(keys, row[:14])); m["rounds"] = json.loads(row[14])
        out.append(m)
    db.close()
    return out


def segment_at(opened):
    """History id of the segment that started at this time (a shuffle round / arena match)."""
    db = connect()
    row = db.execute("SELECT id FROM segments WHERE ABS(opened - ?) < 0.01", (opened,)).fetchone()
    db.close()
    return row[0] if row else None
