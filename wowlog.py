"""Combat log parser, Details!-style: segments -> actors -> spells -> individual hits.

Segments open on ENCOUNTER_START / ARENA_MATCH_START (one per Solo Shuffle round), or
whenever you (or your pets) deal or take damage, and close after COMBAT_GAP seconds of
you being out of combat. While a segment is open, every unit in the log is recorded.
"""
import csv, glob, os, threading, time, collections
from datetime import datetime

LOG_DIR = r"C:\Program Files (x86)\World of Warcraft\_retail_\Logs"
COMBAT_GAP = 6.0
MAX_SEGMENTS = 40
NO_GUID = "0000000000000000"
T_PLAYER = 0x400
HOSTILE = 0x60            # hostile or neutral reaction

SPECS = {}
for cls, specs in {
    "Death Knight": {250: "Blood", 251: "Frost", 252: "Unholy"},
    "Demon Hunter": {577: "Havoc", 581: "Vengeance", 1480: "Devourer"},
    "Druid": {102: "Balance", 103: "Feral", 104: "Guardian", 105: "Restoration"},
    "Evoker": {1467: "Devastation", 1468: "Preservation", 1473: "Augmentation"},
    "Hunter": {253: "Beast Mastery", 254: "Marksmanship", 255: "Survival"},
    "Mage": {62: "Arcane", 63: "Fire", 64: "Frost"},
    "Monk": {268: "Brewmaster", 269: "Windwalker", 270: "Mistweaver"},
    "Paladin": {65: "Holy", 66: "Protection", 70: "Retribution"},
    "Priest": {256: "Discipline", 257: "Holy", 258: "Shadow"},
    "Rogue": {259: "Assassination", 260: "Outlaw", 261: "Subtlety"},
    "Shaman": {262: "Elemental", 263: "Enhancement", 264: "Restoration"},
    "Warlock": {265: "Affliction", 266: "Demonology", 267: "Destruction"},
    "Warrior": {71: "Arms", 72: "Fury", 73: "Protection"},
}.items():
    for sid, name in specs.items():
        SPECS[sid] = (cls, name)

CLASS_COLOR = {"Death Knight": "#C41E3A", "Demon Hunter": "#A330C9", "Druid": "#FF7C0A", "Evoker": "#33937F",
               "Hunter": "#AAD372", "Mage": "#3FC7EB", "Monk": "#00FF98", "Paladin": "#F48CBA", "Priest": "#E8E8E8",
               "Rogue": "#FFF468", "Shaman": "#0070DD", "Warlock": "#8788EE", "Warrior": "#C69B6D"}
# class guess from the power type in the advanced block, for players without COMBATANT_INFO
POWER_CLASS = {6: "Death Knight", 8: "Druid", 11: "Shaman", 13: "Priest", 17: "Demon Hunter", 18: "Demon Hunter",
               9: "Paladin", 12: "Monk", 2: "Hunter", 7: "Warlock", 19: "Evoker"}
POWER = {0: "Mana", 1: "Rage", 2: "Focus", 3: "Energy", 4: "Combo", 6: "Runic", 7: "Shards", 8: "Astral",
         9: "Holy Power", 11: "Maelstrom", 12: "Chi", 13: "Insanity", 17: "Fury", 18: "Pain", 19: "Essence"}
POWER_SCALE = {8: 10}

DAMAGE = {"SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE", "SWING_DAMAGE", "DAMAGE_SHIELD", "DAMAGE_SPLIT"}
HEAL = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
MISS = {"SPELL_MISSED", "SPELL_PERIODIC_MISSED", "SWING_MISSED", "RANGE_MISSED"}
UNIT_PREFIX = ("Player-", "Creature-", "Pet-", "Vehicle-")
RECAP_WINDOW = 20.0       # seconds of history kept per player for death recaps
# recap entries: (t, kind, who, spell, amount, crit, hp%, source hostile, extra)
#   kind: dmg (amount < 0, extra = absorbed part) | heal (extra = overheal) | absorb | cast (extra = target)
#         aura+ / aura- (extra = BUFF/DEBUFF) | kicked (extra = interrupted spell)


def ts_seconds(ts):
    return datetime.strptime(ts[:23], "%m/%d/%Y %H:%M:%S.%f").timestamp()


def push_recent(dq, t, amt, window=5.0):
    dq.append((t, amt))
    while dq[0][0] < t - window:
        dq.popleft()


def short(name):
    return name.split("-")[0] if name and name != "nil" else "?"


class Spell:
    """One ability on one actor. hit_list holds (t, amount, crit, kind, over, absorbed, mitigated, hp%, other)."""
    def __init__(self, name):
        self.name = name
        self.total = self.hits = self.crits = self.ticks = self.over = self.absorbed = 0
        self.n_sum = self.c_sum = self.n_max = self.c_max = 0
        self.n_min = self.c_min = None
        self.misses = collections.Counter()
        self.targets = collections.Counter()
        self.hit_list = []

    def add(self, rec):
        t, amt, crit, kind, over, absorbed, mit, hp, other = rec
        self.total += amt; self.hits += 1
        if crit:
            self.crits += 1; self.c_sum += amt; self.c_max = max(self.c_max, amt)
            self.c_min = amt if self.c_min is None else min(self.c_min, amt)
        else:
            self.n_sum += amt; self.n_max = max(self.n_max, amt)
            self.n_min = amt if self.n_min is None else min(self.n_min, amt)
        if kind in ("DoT", "HoT"):
            self.ticks += 1
        self.over += over; self.absorbed += absorbed
        self.targets[other] += amt
        self.hit_list.append(rec)


class Actor:
    def __init__(self, key, name, hostile):
        self.key, self.name, self.hostile = key, name, hostile
        self.is_player = key.startswith("Player-")
        self.dmg, self.heal, self.taken, self.ff = {}, {}, {}, {}   # ff = friendly fire (incl. own reflected spells)
        self.dmg_total = self.heal_total = self.overheal = self.taken_total = self.ff_total = 0
        self.casts, self.interrupts, self.dispels = [], [], []
        self.deaths = 0
        self.cls = self.spec = None      # set on actors loaded from history
        self.recent_dmg = collections.deque()    # (t, amount) for the rolling "now" numbers
        self.buckets = collections.Counter()      # whole second -> damage done (graphs)
        self.hbuckets = collections.Counter()     # whole second -> healing done
        self.recent_heal = collections.deque()

    def rate(self, kind, now, window=5.0):
        dq = self.recent_dmg if kind == "dmg" else self.recent_heal
        while dq and dq[0][0] < now - window:
            dq.popleft()
        return sum(amt for _, amt in dq) / window

    @staticmethod
    def spell(book, key):
        s = book.get(key)
        if s is None:
            s = book[key] = Spell(key)
        return s


class Segment:
    def __init__(self, name, t, forced=False, overall=False):
        self.name, self.opened, self.forced = name, t, forced
        self.start = None          # first damage
        self.last = t
        self.actors = {}
        self.deaths = []
        self.closed = False
        self.parts = [] if overall else None
        self.extra_time = 0.0

    @property
    def duration(self):
        if self.parts is not None:
            return max(self.extra_time + sum(p.duration for p in self.parts), 1.0)
        return max(self.last - self.start, 1.0) if self.start else 1.0

    @property
    def t0(self):
        return self.start or self.opened

    def label(self):
        if self.parts is not None:
            return "Overall"
        name = self.name
        if not name:
            foes = [a for a in self.actors.values() if a.hostile and a.taken_total]
            name = max(foes, key=lambda a: a.taken_total).name if foes else "Combat"
        m, s = divmod(int(self.duration), 60)
        return f"{datetime.fromtimestamp(self.opened):%H:%M}  {name}  ({m}:{s:02d})"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.log_file = None
        self.progress = 0.0
        self.lines = 0
        self.me = self.my_name = None
        self.owners = {}           # pet/guardian guid -> owner guid
        self.names = {}            # player guid -> name
        self.classes = {}          # player guid -> (class, spec or None)
        self.recent = {}           # player guid -> deque of incoming events (death recap)
        self.round = 0
        self.log_now = 0
        self.delay = None           # seconds between a line's timestamp and us reading it
        self.last_read = time.time()  # wall time new log data last arrived
        self.on_close = None       # callback(segment) - the history store hooks in here
        self.on_match = None       # callback(match dict) when an arena match ends
        self.zone = None
        self.match = None          # arena match in progress
        self.matches = []          # finished matches this session
        self.cur = None
        self.reset()

    def reset(self):
        self.close()               # the open segment gets saved before it is dropped
        self.segments = []
        self.overall = Segment("Overall", 0, overall=True)
        self.cur = None
        self.last_inv = 0

    # ------------------------------------------------------------- segments
    def open(self, name, t, forced=False):
        self.close()
        self.cur = Segment(name, t, forced)
        self.segments.append(self.cur)
        self.overall.parts.append(self.cur)
        if len(self.segments) > MAX_SEGMENTS:
            old = self.segments.pop(0)
            self.overall.parts.remove(old)
            self.overall.extra_time += old.duration

    def close(self):
        if self.cur:
            self.cur.closed = True
            if self.on_close:
                self.on_close(self.cur)
            self.cur = None

    def is_me(self, guid):
        return guid == self.me or self.owners.get(guid) == self.me

    def actor(self, seg, guid, name, flags):
        """Returns (actor, pet name or None). Pets fold into owners, NPCs merge by name."""
        owner = self.owners.get(guid)
        pet = None
        if owner and not guid.startswith("Player-"):
            key, aname, pet = owner, self.names.get(owner, name), short(name)
        elif guid.startswith("Player-"):
            key, aname = guid, name
        else:
            key, aname = "npc:" + name, name
        a = seg.actors.get(key)
        if a is None:
            a = seg.actors[key] = Actor(key, short(aname) if key.startswith("Player-") else aname,
                                        bool(flags & HOSTILE))
        return a, pet

    def color(self, a):
        c = self.classes.get(a.key) or ((a.cls, a.spec) if a.cls else None)
        if c:
            return CLASS_COLOR.get(c[0], "#7f8c99")
        if not a.is_player:
            return "#a83f3f" if a.hostile else "#5b6b7b"
        return "#7f8c99"

    def spec(self, a):
        c = self.classes.get(a.key) or ((a.cls, a.spec) if a.cls else None)
        return f"{c[1]} {c[0]}" if c and c[1] else (c[0] if c else ("Player" if a.is_player else "NPC"))

    # ------------------------------------------------------------- parsing
    def feed(self, line):
        try:
            ts, rest = line.split("  ", 1)
            r = next(csv.reader([rest]))
            t = ts_seconds(ts)
        except (ValueError, StopIteration):
            return
        self.lines += 1
        self.log_now = t
        ev = r[0]
        if self.cur and not self.cur.forced and t - self.last_inv > COMBAT_GAP:
            self.close()

        if ev == "COMBAT_LOG_VERSION":          # logging (re)started - possibly on another character
            self.close()
            self.me = self.my_name = None
            self.match = None
            return
        if ev == "ZONE_CHANGE":
            if self.match:                     # left the arena without a match end (leaver / disconnect)
                self.match = None
            self.zone = r[2]; return
        if ev == "ARENA_MATCH_START":
            shuffle = "Shuffle" in r[3]
            self.round = self.round + 1 if shuffle else 0
            self.open(f"{r[3]} · round {self.round}" if shuffle else r[3], t, True)
            if self.match is None:
                self.match = {"start": t, "type": r[3], "zone": self.zone, "team": r[4], "rounds": []}
            self.match["rounds"].append(self.cur)
            return
        if ev == "ENCOUNTER_START":
            self.open(r[2], t, True); return
        if ev in ("ARENA_MATCH_END", "ENCOUNTER_END"):
            self.close()
            if ev == "ARENA_MATCH_END":
                self.round = 0
                if self.match:
                    self.finish_match(t, r)
            return
        if ev == "COMBATANT_INFO":
            try:
                spec = SPECS.get(int(r[25]))
                if spec:
                    self.classes[r[1]] = spec
            except (ValueError, IndexError):
                pass
            return
        if len(r) < 9 or not r[3].startswith("0x"):
            return

        src, sname, sf = r[1], r[2], int(r[3], 16)
        dst, dname, df = r[5], r[6], int(r[7], 16)
        if src.startswith("Player-"):
            self.names[src] = sname
        if self.me is None and sf & 0x1 and sf & T_PLAYER and src.startswith("Player-"):
            self.me, self.my_name = src, sname
        if ev == "SPELL_SUMMON":
            self.owners[dst] = src

        # advanced block: owner of pets, class guess from power type
        i = 9 if ev.startswith("SWING_") else 12
        adv = len(r) > i + 18 and r[i].startswith(UNIT_PREFIX)
        if adv:
            unit, owner = r[i], r[i + 1]
            if owner != NO_GUID and not unit.startswith("Player-"):
                self.owners.setdefault(unit, owner)
            if unit.startswith("Player-") and unit not in self.classes:
                c = POWER_CLASS.get(int(r[i + 10])) if r[i + 10].lstrip("-").isdigit() else None
                if c:
                    self.classes[unit] = (c, None)

        if dst.startswith("Player-"):
            if ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED") and len(r) > 12:
                self.remember(dst, (t, "aura+" if ev == "SPELL_AURA_APPLIED" else "aura-", short(sname), r[10],
                                    0, False, None, bool(sf & HOSTILE), r[12]))
            elif ev == "SPELL_INTERRUPT" and len(r) > 13:
                self.remember(dst, (t, "kicked", short(sname), r[10], 0, False, None, bool(sf & HOSTILE), r[13]))

        absorb_miss = ev in MISS and (r[9] if ev == "SWING_MISSED" else r[12]) == "ABSORB"
        if ev in DAMAGE or absorb_miss:
            ms, md = self.is_me(src), self.is_me(dst)
            if ms != md:
                self.last_inv = t
                if self.cur is None:
                    self.open(None, t)
            if absorb_miss:
                self.on_absorbed_hit(ev, r, t, src, sname, sf, dst, dname, df)
            else:
                self.on_damage(ev, r, t, i if adv else None, src, sname, sf, dst, dname, df)
        elif ev in HEAL:
            self.on_heal(ev, r, t, i if adv else None, src, sname, sf, dst, dname, df)
        elif self.cur is None:
            return
        elif ev == "SPELL_ABSORBED":
            self.on_absorb(r, t, dst, dname)
        elif ev in MISS:
            mt = r[9] if ev == "SWING_MISSED" else r[12]
            key = "Melee" if ev == "SWING_MISSED" else r[10]
            for seg in (self.cur, self.overall):
                a, pet = self.actor(seg, src, sname, sf)
                Actor.spell(a.dmg, f"{key} ({pet})" if pet else key).misses[mt] += 1
        elif ev == "SPELL_CAST_SUCCESS" and src.startswith("Player-"):
            power = ""
            if adv and r[i] == src:
                try:
                    pt = int(r[i + 10]); sc = POWER_SCALE.get(pt, 1)
                    power = f"{int(r[i + 11]) // sc}/{int(r[i + 12]) // sc} {POWER.get(pt, '')}"
                except ValueError:
                    pass
            rec = (t, r[10], "" if dname == "nil" else short(dname), power)
            for seg in (self.cur, self.overall):
                self.actor(seg, src, sname, sf)[0].casts.append(rec)
            self.remember(src, (t, "cast", "", r[10], 0, False, self.hp_of(r, i if adv else None, src), False, rec[2]))
        elif ev == "SPELL_INTERRUPT":
            rec = (t, r[10], short(dname), r[13])
            for seg in (self.cur, self.overall):
                self.actor(seg, src, sname, sf)[0].interrupts.append(rec)
        elif ev in ("SPELL_DISPEL", "SPELL_STOLEN"):
            rec = (t, r[10], short(dname), r[13])
            for seg in (self.cur, self.overall):
                self.actor(seg, src, sname, sf)[0].dispels.append(rec)
        elif ev == "UNIT_DIED" and df & T_PLAYER and dst.startswith("Player-") and r[-1] != "1":
            recap = list(self.recent.get(dst, ()))
            blow = next((e for e in reversed(recap) if e[1] == "dmg"), None)
            death = {"t": t, "key": dst, "name": short(dname), "hostile": bool(df & HOSTILE), "recap": recap,
                     "killer": blow[2] if blow else "?", "blow": f"{blow[3]} ({-blow[4]:,})" if blow else ""}
            for seg in (self.cur, self.overall):
                seg.deaths.append(death)
                self.actor(seg, dst, dname, df)[0].deaths += 1

    # ------------------------------------------------------------- arena results
    def team_list(self, actors):
        return [{"name": a.name, "spec": self.spec(a), "cls": (self.classes.get(a.key) or (None,))[0],
                 "me": a.key == self.me} for a in sorted(actors, key=lambda a: a.key != self.me)]

    def finish_match(self, t, r):
        """Shuffle: each round is decided by its first death. 2v2/3v3: by the last death, else by the winning team."""
        m, self.match = self.match, None
        try:
            winner, server_dur, rating1, rating2 = int(r[1]), int(r[2]), int(r[3]), int(r[4])
        except (ValueError, IndexError):
            winner, server_dur, rating1, rating2 = None, None, None, None
        shuffle = "Shuffle" in m["type"]
        rounds = []
        for seg in m["rounds"]:
            players = [a for a in seg.actors.values() if a.is_player]
            friends, foes = [a for a in players if not a.hostile], [a for a in players if a.hostile]
            deaths = seg.deaths
            decisive = (deaths[0] if shuffle else deaths[-1]) if deaths else None
            if decisive:
                result = "win" if decisive["hostile"] else "loss"
            elif not shuffle and winner in (0, 1) and m["team"].lstrip("-").isdigit():
                result = "win" if winner == int(m["team"]) else "loss"
            else:
                result = "draw"
            me = seg.actors.get(self.me)
            rounds.append({"opened": seg.opened, "duration": seg.duration, "result": result,
                           "died": decisive["name"] if decisive else None,
                           "killer": decisive["killer"] if decisive else None,
                           "team": self.team_list(friends), "enemies": self.team_list(foes),
                           "my_dmg": me.dmg_total if me else 0, "my_heal": me.heal_total if me else 0})
        if not rounds:
            return
        rw = sum(x["result"] == "win" for x in rounds); rl = sum(x["result"] == "loss" for x in rounds)
        n = max(len(rounds[0]["team"]), len(rounds[0]["enemies"]), 1)
        bracket = ("Solo Shuffle" if shuffle else "Skirmish" if "Skirmish" in m["type"] else f"{n}v{n}")
        match = {"start": m["start"], "end": t, "type": m["type"], "bracket": bracket, "zone": m["zone"],
                 "result": "win" if rw > rl else "loss" if rl > rw else "even", "rw": rw, "rl": rl,
                 "rd": len(rounds) - rw - rl, "duration": server_dur if server_dur and not shuffle else t - m["start"],
                 "ratings": [rating1, rating2], "winner": winner, "rounds": rounds,
                 "me_key": self.me, "me_name": self.my_name}
        self.matches.append(match)
        if self.on_match:
            self.on_match(match)

    def who(self, guid, name):
        """Display name for recaps: pets/guardians show their owner, e.g. 'Arcane Phoenix (Infocus)'."""
        owner = self.owners.get(guid)
        if owner and not guid.startswith("Player-"):
            return f"{short(name)} ({short(self.names.get(owner, '?'))})"
        return short(name)

    def remember(self, guid, rec):
        if guid.startswith("Player-"):
            d = self.recent.get(guid)
            if d is None:
                d = self.recent[guid] = collections.deque(maxlen=1500)
            d.append(rec)
            while d[0][0] < rec[0] - RECAP_WINDOW:
                d.popleft()

    def hp_of(self, r, i, guid):
        if i is not None and r[i] == guid:
            try:
                mx = int(r[i + 3])
                return round(100 * int(r[i + 2]) / mx, 1) if mx else None
            except ValueError:
                pass
        return None

    def on_damage(self, ev, r, t, i, src, sname, sf, dst, dname, df):
        off = 1 if r[-1] in ("ST", "AOE") else 0
        try:
            amt, base, over = int(r[-10 - off]), int(r[-9 - off]), int(r[-8 - off])
            absorbed, crit = int(r[-4 - off]), r[-3 - off] == "1"
        except (ValueError, IndexError):
            return
        spell = "Melee" if ev == "SWING_DAMAGE" else r[10]
        kind = "DoT" if ev == "SPELL_PERIODIC_DAMAGE" else ("AoE" if r[-1] == "AOE" else "Direct")
        # damage into shields counts as damage done (like Details / the in-game meter)
        self.add_damage(t, src, sname, sf, dst, dname, df, spell, amt + absorbed, crit, kind,
                        max(over, 0), absorbed, max(base - amt - absorbed, 0), self.hp_of(r, i, dst))

    def on_absorbed_hit(self, ev, r, t, src, sname, sf, dst, dname, df):
        """A hit fully eaten by a shield: logged as *_MISSED with type ABSORB and the absorbed amount."""
        mi = 9 if ev == "SWING_MISSED" else 12
        try:
            amt = int(r[mi + 2])
        except (ValueError, IndexError):
            return
        crit = len(r) > mi + 4 and r[mi + 4] == "1"
        spell = "Melee" if ev == "SWING_MISSED" else r[10]
        kind = "DoT" if ev == "SPELL_PERIODIC_MISSED" else "Absorbed"
        self.add_damage(t, src, sname, sf, dst, dname, df, spell, amt, crit, kind, 0, amt, 0, None)

    def add_damage(self, t, src, sname, sf, dst, dname, df, spell, amt, crit, kind, over, absorbed, mit, hp):
        self.remember(dst, (t, "dmg", self.who(src, sname), spell, -amt, crit, hp, bool(sf & HOSTILE), absorbed))
        if self.cur is None:
            return
        for seg in (self.cur, self.overall):
            if seg.start is None:
                seg.start = t
            seg.last = t
            a, pet = self.actor(seg, src, sname, sf)
            rec = (t, amt, crit, kind, over, absorbed, mit, hp, short(dname))
            if bool(sf & HOSTILE) == bool(df & HOSTILE):        # same side: not counted as damage done
                a.ff_total += amt
                Actor.spell(a.ff, f"{spell} ({pet})" if pet else spell).add(rec)
            else:
                a.dmg_total += amt
                push_recent(a.recent_dmg, t, amt)
                a.buckets[int(t)] += amt
                Actor.spell(a.dmg, f"{spell} ({pet})" if pet else spell).add(rec)
            if not (self.owners.get(dst) and not dst.startswith("Player-")):
                d, _ = self.actor(seg, dst, dname, df)
                d.taken_total += amt
                Actor.spell(d.taken, f"{spell} — {short(sname)}").add(
                    (t, amt, crit, kind, over, absorbed, mit, hp, short(sname)))

    def on_heal(self, ev, r, t, i, src, sname, sf, dst, dname, df):
        off = 1 if r[-1] in ("ST", "AOE") else 0
        try:
            amt, over, absorbed = int(r[-5 - off]), int(r[-3 - off]), int(r[-2 - off])
            crit = r[-1 - off] == "1"
        except (ValueError, IndexError):
            return
        eff = max(amt - over, 0)
        hp = self.hp_of(r, i, dst)
        if eff or hp is not None:
            self.remember(dst, (t, "heal", self.who(src, sname), r[10], eff, crit, hp, bool(sf & HOSTILE), over))
        if self.cur is None:
            return
        kind = "HoT" if ev == "SPELL_PERIODIC_HEAL" else "Direct"
        for seg in (self.cur, self.overall):
            a, pet = self.actor(seg, src, sname, sf)
            a.heal_total += eff; a.overheal += over
            push_recent(a.recent_heal, t, eff)
            a.hbuckets[int(t)] += eff
            Actor.spell(a.heal, f"{r[10]} ({pet})" if pet else r[10]).add(
                (t, eff, crit, kind, over, absorbed, 0, hp, short(dname)))

    def on_absorb(self, r, t, dst, dname):
        try:
            caster, cname, cflags, shield, amt = r[-10], r[-9], int(r[-8], 16), r[-5], int(r[-3])
        except (ValueError, IndexError):
            return
        self.remember(dst, (t, "absorb", self.who(caster, cname), shield, amt, False, None, bool(cflags & HOSTILE), 0))
        for seg in (self.cur, self.overall):
            a, pet = self.actor(seg, caster, cname, cflags)
            a.heal_total += amt
            push_recent(a.recent_heal, t, amt)
            a.hbuckets[int(t)] += amt
            Actor.spell(a.heal, f"{shield} ({pet})" if pet else shield).add(
                (t, amt, False, "Absorb", 0, 0, 0, None, short(dname)))


def newest_log():
    files = glob.glob(os.path.join(LOG_DIR, "WoWCombatLog-*.txt"))
    return max(files, key=os.path.getmtime) if files else None


def start_tail(state, replay=True):
    def run():
        fh, path, buf = None, None, ""
        while True:
            latest = newest_log()
            if latest and latest != path:
                if fh:
                    fh.close()
                path, buf = latest, ""
                fh = open(path, encoding="utf-8", errors="replace")
                if not replay and state.log_file is None:
                    fh.seek(0, os.SEEK_END)
                with state.lock:
                    state.log_file = path
            if fh:
                chunk = fh.read(4_000_000)
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for n in range(0, len(lines), 2000):      # let the UI grab the lock in between
                        with state.lock:
                            for line in lines[n:n + 2000]:
                                state.feed(line.rstrip("\r"))
                    state.last_read = time.time()
                    caught_up = state.progress > 0.999          # only time chunks that arrive live
                    state.progress = fh.tell() / max(os.path.getsize(path), 1)
                    if caught_up and state.log_now:
                        lag = max(time.time() - state.log_now, 0)
                        state.delay = lag if state.delay is None else 0.7 * state.delay + 0.3 * lag
                    continue
                state.progress = 1.0
            time.sleep(0.1)
    threading.Thread(target=run, daemon=True).start()
