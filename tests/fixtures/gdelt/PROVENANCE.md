# GDELT 2.0 fixtures (M6.1)

Frozen real payloads from `data.gdeltproject.org`, fetched live on **2026-09-02** with a browser
User-Agent. Tests parse these; the suite never touches the network (AGENTIC_CONTEXT §8 / B8).

| File | Source URL | Bytes | sha256 |
|---|---|---|---|
| `v2/lastupdate.txt` | `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` | 319 | `d423410167420eeeba357278d3e663376ac4689abcb92dba2b22da4fee76dd0c` |
| `v2/20260902074500.export.CSV.zip` | `http://data.gdeltproject.org/gdeltv2/20260902074500.export.CSV.zip` | 75093 | `228a1e3f6b4c2eeeb6f78868325867bfa9d3063d837dbc8d913bcd1a979dcf3e` |

- The manifest (`lastupdate.txt`) names three files of the 20260902074500 slot with their MD5s.
  The export line's MD5 is `99034aac15390b75b102dd54d797bea6`, which matches the frozen zip and is
  cross-checked by `dataplatform.ingest.gdelt` on ingest.
- The export CSV holds **1210 events**, 61 tab-separated columns each (GDELT 2.0 export schema).
  The columns this parser reads: Actor1Name (6), Actor2Name (16), AvgTone (34), DATEADDED (59,
  `YYYYMMDDHHMMSS` UTC), SOURCEURL (60). 0-indexed.
- Access is over plain HTTP by necessity: the host is a CNAME to Google Cloud Storage and presents
  a certificate for that name, so HTTPS fails validation (recorded in `source_register.yaml`).
