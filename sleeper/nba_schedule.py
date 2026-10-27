"""The NBA schedule for the CURRENT fantasy week.

Why the bot needs this: this league uses WEEKLY lineups. A starting slot earns
every game its player plays that week, and a player locks into his slot
(starter or bench) the moment his first game of the week tips off. So the
value of starting someone is

    per-game fantasy points  x  games he has LEFT this week

— and whether he happens to play "tonight" is irrelevant. Without a schedule the
bot can't count games left, which is how it ended up starting whoever played
tonight (who then locked in for the week) over stars who played later.

Two public sources, tried in order, both best-effort:
  1. NBA's own static season schedule  (cdn.nba.com scheduleLeagueV2.json)
  2. ESPN's per-day scoreboard         (site.api.espn.com), one call per day
If both fail, `known` stays False and every caller falls back to ranking by
per-game value alone (i.e. treats every player as having the same games left).
That fallback is still far better than the old "who plays tonight" logic, and a
schedule outage can never break a run.

Matchup weeks are treated as Monday–Sunday in US Eastern time, which matches
Sleeper's NBA weeks (and this league's data: week 2's first locks happened on
Monday). Every date here is an Eastern calendar date.
"""

import datetime as dt
import time

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — tz database missing; close enough
    ET = dt.timezone(dt.timedelta(hours=-5))

NBA_CDN_SCHEDULE = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
                   "scoreboard?dates={ymd}&limit=100")
# A realistic browser identity. The first attempt from the GitHub Actions runner
# failed two ways that both trace back to headers/size: ESPN returned 403 (its
# WAF blocks non-browser user-agents from datacenter IPs) and cdn.nba.com's full
# season file (~4 MB) read-timed-out at 20s. So: look like Chrome, accept gzip
# (the file compresses to ~1 MB), and give each source a longer read window with
# one retry. Per-site Referer/Origin added at call time.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
_HEADERS = {
    "user-agent": _UA,
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",
    "cache-control": "no-cache",
}
_CONNECT_TIMEOUT = 10
_RETRIES = 1          # one extra attempt per source on a transient failure

# Sources (and Sleeper) don't all agree on team codes — normalize everything to
# one canonical tricode so a player's `team` always matches a schedule entry.
_ALIASES = {
    "GS": "GSW", "GOS": "GSW", "NO": "NOP", "NOR": "NOP", "NOH": "NOP",
    "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "UTH": "UTA", "WSH": "WAS",
    "PHO": "PHX", "BRK": "BKN", "BKLYN": "BKN", "NJ": "BKN", "NJN": "BKN",
    "CHO": "CHA", "CHH": "CHA",
}

# A real NBA week has ~45-55 games touching all 30 teams. Fewer distinct teams
# than this means the payload is wrong/partial, so we refuse to trust it.
_MIN_TEAMS_FOR_TRUST = 12


def norm_team(code):
    if not code:
        return None
    c = str(code).strip().upper()
    return _ALIASES.get(c, c)


def _is_hard_reject(e):
    """A 403/404 won't improve on retry; timeouts and 5xx might."""
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (403, 404):
        return True
    m = str(e).lower()
    return "403" in m or "404" in m or "forbidden" in m or "not found" in m


def _parse_utc(value):
    """Parse an ISO timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def week_window(now_et):
    """(week_start, week_end, today) as Eastern dates for the Mon–Sun week
    containing `now_et`."""
    today = now_et.date()
    start = today - dt.timedelta(days=today.weekday())   # Monday
    return start, start + dt.timedelta(days=6), today


def _nba_game_date(game, game_date_block):
    """The Eastern calendar date of an NBA CDN game entry."""
    est = game.get("gameDateEst")
    if est and len(str(est)) >= 10:
        try:
            return dt.date.fromisoformat(str(est)[:10])
        except ValueError:
            pass
    gd = (game_date_block or {}).get("gameDate")          # "10/27/2026 00:00:00"
    if gd:
        try:
            return dt.datetime.strptime(str(gd).split(" ")[0], "%m/%d/%Y").date()
        except ValueError:
            pass
    t = _parse_utc(game.get("gameDateTimeUTC"))
    return t.astimezone(ET).date() if t else None


def parse_nba_cdn(data, start, end):
    """[(date_et, start_utc, home, away)] for games in [start, end]."""
    out = []
    blocks = ((data or {}).get("leagueSchedule") or {}).get("gameDates") or []
    for block in blocks:
        for g in block.get("games") or []:
            d = _nba_game_date(g, block)
            if d is None or not (start <= d <= end):
                continue
            home = norm_team((g.get("homeTeam") or {}).get("teamTricode"))
            away = norm_team((g.get("awayTeam") or {}).get("teamTricode"))
            if home and away:
                out.append((d, _parse_utc(g.get("gameDateTimeUTC")), home, away))
    return out


def parse_espn(data, day):
    """[(date_et, start_utc, home, away)] from one ESPN scoreboard day."""
    out = []
    for ev in (data or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0] or {}
        teams = {}
        for c in comp.get("competitors") or []:
            side = c.get("homeAway")
            abbr = norm_team((c.get("team") or {}).get("abbreviation"))
            if side in ("home", "away") and abbr:
                teams[side] = abbr
        if "home" in teams and "away" in teams:
            out.append((day, _parse_utc(ev.get("date")), teams["home"], teams["away"]))
    return out


class WeekSchedule:
    """Games for the current Mon–Sun fantasy week. Call .load() once per run."""

    def __init__(self, now=None, session=None, timeout=20):
        self.now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        self.week_start, self.week_end, self.today = week_window(self.now.astimezone(ET))
        self.games = []
        self.known = False
        self.source = None
        self.error = None
        self._http = session or requests
        self._timeout = timeout          # read timeout for the per-day ESPN calls
        self._sleep = time.sleep

    # ---- loading ----
    def _get_json(self, url, read_timeout, referer=None):
        """GET JSON with browser headers, a connect+read timeout, and one retry
        on a transient failure (timeout / connection reset / 5xx). A 403 or 404
        is not retried — more tries won't change a hard rejection."""
        headers = dict(_HEADERS)
        if referer:
            headers["referer"] = referer
            headers["origin"] = referer.rstrip("/")
        last = None
        for attempt in range(_RETRIES + 1):
            try:
                r = self._http.get(url, headers=headers,
                                   timeout=(_CONNECT_TIMEOUT, read_timeout))
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                if _is_hard_reject(e) or attempt >= _RETRIES:
                    raise
                self._sleep(1.5)
        raise last

    def _load_nba_cdn(self):
        # One request for the whole season (so mid-week games-left is accurate),
        # but it's a big file — give it a long read window.
        data = self._get_json(NBA_CDN_SCHEDULE, read_timeout=45,
                               referer="https://www.nba.com/")
        return parse_nba_cdn(data, self.week_start, self.week_end)

    def _load_espn(self):
        out, day = [], self.week_start
        while day <= self.week_end:           # all-or-nothing: a missing day would
            data = self._get_json(ESPN_SCOREBOARD.format(ymd=day.strftime("%Y%m%d")),
                                  read_timeout=self._timeout,
                                  referer="https://www.espn.com/")
            out.extend(parse_espn(data, day))  # silently undercount games left
            day += dt.timedelta(days=1)
        return out

    def load(self):
        errors = []
        for name, loader in (("nba-cdn", self._load_nba_cdn), ("espn", self._load_espn)):
            try:
                games = loader()
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            teams = {t for _, _, h, a in games for t in (h, a)}
            if len(teams) < _MIN_TEAMS_FOR_TRUST:
                errors.append(f"{name}: only {len(teams)} teams this week — not trusted")
                continue
            self.games, self.known, self.source = games, True, name
            return self
        self.error = "; ".join(errors) or "no source"
        return self

    # ---- queries ----
    def _tipped_off(self, day, start):
        """Has a game on `day` (starting at `start`) already begun?"""
        if day < self.today:
            return True
        if day > self.today:
            return False
        return start is not None and start <= self.now

    def games_left(self, team):
        """Games this team still has this week that haven't tipped off. None when
        the schedule is unknown; 0 for a player with no NBA team."""
        if not self.known:
            return None
        t = norm_team(team)
        if not t:
            return 0
        return sum(1 for d, s, h, a in self.games
                   if t in (h, a) and not self._tipped_off(d, s))

    def started_this_week(self, team):
        """True if this team has already tipped off at least once this week —
        i.e. its players are locked into their current slot for the week."""
        if not self.known:
            return False
        t = norm_team(team)
        if not t:
            return False
        return any(t in (h, a) and self._tipped_off(d, s) for d, s, h, a in self.games)

    def days_left(self):
        """Calendar days left in the matchup week, including today."""
        return (self.week_end - self.today).days + 1

    def summary(self):
        if not self.known:
            return f"schedule unavailable ({self.error}) — ranking by per-game value"
        return (f"schedule {self.source}: {len(self.games)} games "
                f"{self.week_start:%a %b %d}–{self.week_end:%a %b %d}, "
                f"{self.days_left()} day(s) left")


def load_week_schedule(now=None):
    """Best-effort one-liner used by the autopilot. Never raises; on any failure
    returns a schedule with known=False, which every caller handles."""
    sched = WeekSchedule(now=now)
    try:
        sched.load()
    except Exception as e:  # pragma: no cover — load() already catches per-source
        sched.known, sched.games, sched.error = False, [], str(e)
    return sched
