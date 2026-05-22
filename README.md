# Activity Suggester

Constraint-first activity browser for 150+ hobbies, projects, chores, and relaxation activities. Filter by time, energy, weather, location — visual sunburst drill-down.

## Structure

```
server/          Python backend (Flask API + CLI)
  activity-server.py    Flask server on :8092
  activity-suggest.py   CLI (suggest, done, stats, browse, rebuild)
  browse_core.py        Query engine, gates, scoring
  browse_session.py     Stateful session wrapper
  activity_rhythm.py    Time-of-day energy/mental schedule

static/          Frontend
  index.html           D3 sunburst browser (served by Flask)

skills/          Hermes agent interface
  SKILL.md             Conversational skill for Hermes

config/          Deployment
  activity-browser.service   systemd unit
```

## Running

```bash
# Start server
systemctl --user start activity-browser.service

# CLI
activity-suggest suggest --time 60 --energy low
activity-suggest done --done "Archery" --duration 90
activity-suggest stats --days 7

# Open browser
http://localhost:8092
```

## API

| Endpoint | Description |
|----------|-------------|
| GET /api/v1/state | Current rhythm (day, hour, energy, mental) |
| GET /api/v1/filters | Filter definitions (extensible config) |
| GET /api/v1/activities | All 147 activities with metadata |
| GET /api/v1/query?time=30&intent=make | Filtered results |
| POST /api/v1/log | Log completed activity |
| GET /api/v1/stats | Recency history |
| POST /api/v1/surprise | Random weighted pick |
