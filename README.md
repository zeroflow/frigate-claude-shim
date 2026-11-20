# frigate-claude-shim

A tiny, stdlib-only proxy that lets [Frigate](https://frigate.video)'s GenAI **review descriptions**
run on **Anthropic Claude** through Anthropic's OpenAI-compatible endpoint. It also gives the model
context that Frigate's prompt doesn't include: the active Frigate profile, today's date, and
sunrise/sunset.

```
Frigate (provider: openai) ──▶ claude-shim:8080 ──▶ https://api.anthropic.com/v1/chat/completions
```

## Why

If you point Frigate's `openai` provider at `https://api.anthropic.com/v1/`, plain chat works.
Review descriptions fail on every request:

```
OpenAI returned an error: Error code: 400 - {'error': {'code': 'invalid_request_error', 'message':
"response_format.json_schema.schema: For 'array' type, 'minItems' values other than 0 or 1 are not supported ..."}}
```

Frigate sends a **strict** `json_schema` with `minItems: 3`, `maxItems`, `minLength`/`maxLength`
and `minimum`/`maximum`. Anthropic's strict structured outputs reject these keywords, so you get
no description at all.

The shim fixes this and adds a few things:

| | |
|---|---|
| **Schema relaxing** | Removes the unsupported keywords and writes them into the field's `description`, so the model still sees them. Frigate soft-validates the answer anyway and keeps one that is slightly out of range. |
| **Empty-field retry** | Without `minLength`, strict decoding sometimes returns `""` for a required text field (seen with `scene`). The shim retries such a request once. |
| **Context enrichment** *(optional)* | Adds lines under `## Sequence Details` in the review prompt: active Frigate profile (`home` / `away` / `night` …), a clock note (which zone "Activity started at" is in, local UTC offset), today's date, and sunrise/sunset for your location. Frigate itself sends only weekday + time, so "after dark" or "nobody is home" is otherwise a guess. |
| **`anthropic-version` header** | Makes `GET /v1/models` work. |
| **Not an open proxy** | Only `POST /v1/chat/completions` and `GET /v1/models` pass; the upstream host is pinned; redirects are not followed. |

The API key passes through in the `Authorization` header and is never logged. Each request is
logged as one line: method, path, status, duration, token counts.

Tested with Frigate 0.18.2 and `claude-haiku-5-5` / `claude-sonnet-5-5`.

## Setup

### 1. Compose

Put `shim.py` next to your compose file (or anywhere) and add a service to Frigate's stack.

```yaml
services:
  frigate:
    # ... your existing Frigate service
    environment:
      FRIGATE_ANTHROPIC_API_KEY: "${FRIGATE_ANTHROPIC_API_KEY}"

  claude-shim:
    container_name: claude-shim
    image: python:3.13-alpine
    restart: unless-stopped
    command: ["python3", "-u", "/app/shim.py"]
    user: "65534:65534"
    read_only: true
    environment:
      FRIGATE_URL: http://frigate:5000   # optional: active profile (Frigate's internal port)
      LAT: "48.21"                       # optional: sunrise/sunset (town-level precision is plenty)
      LON: "16.37"
      LOCAL_TZ: Europe/Vienna            # IANA zone of the property
      # FRIGATE_TZ: UTC                  # zone Frigate formats prompt times in (UTC unless you set TZ on Frigate)
    volumes:
      - ./shim.py:/app/shim.py:ro
```

Don't publish a port. Frigate reaches the shim over the stack network as `http://claude-shim:8080`.

### 2. Frigate config

```yaml
genai:
  claude:
    provider: openai
    base_url: http://claude-shim:8080/v1/
    api_key: "{FRIGATE_ANTHROPIC_API_KEY}"
    model: claude-haiku-5-5            # or claude-sonnet-5-5
    roles:
      - descriptions
    provider_options:
      context_size: 200000

review:
  genai:
    enabled: false                     # enable per camera below
    alerts: true
    detections: false
    preferred_language: English
    activity_context_prompt: |
      ... see examples/activity_context_prompt.md ...

cameras:
  front_door:
    review:
      genai:
        enabled: true
```

Notes:

- `context_size` skips Frigate's model lookup.
- Leave `chat` out of `roles` if some cameras must stay local. Frigate's chat tools can fetch
  snapshots from **any** camera.
- Add `embeddings` only if you know what you're doing. Anthropic has no embeddings API.

### 3. Check

```sh
docker logs -f claude-shim
# POST '/v1/chat/completions' 200 6.2s in=4009 out=651
```

With `FRIGATE_URL`, `LAT` and `LON` set, the review prompt gets lines like these:

```
## Sequence Details

- Active Frigate profile: away (Away)
- Clock: "Activity started at" is in UTC. Local time (Europe/Vienna) is UTC+01:00 today. Times in the property description are local time.
- Today: Friday 2026-11-20, now 14:34 UTC (02:34 PM) = 15:34 local
- Sunrise today 06:08 UTC (06:08 AM) = 07:08 local; sunset today 15:11 UTC (03:11 PM) = 16:11 local
- Camera: Front Door
...
```

Your `activity_context_prompt` should tell the model what the profiles mean and to judge darkness
from these sunrise/sunset times, not from image brightness. See
[`examples/activity_context_prompt.md`](examples/activity_context_prompt.md).

### Firewall note

If a host firewall allows only certain sources to reach Frigate's unauthenticated port 5000,
give the stack network a fixed subnet and the shim a fixed IP. Then allow only that `/32`:

```yaml
services:
  claude-shim:
    networks:
      default:
        ipv4_address: 172.30.7.10
networks:
  default:
    name: frigate_net
    ipam:
      config:
        - subnet: 172.30.7.0/24
```

## Environment

| Variable | Default | |
|---|---|---|
| `PORT` | `8080` | listen port inside the container |
| `UPSTREAM` | `https://api.anthropic.com` | |
| `ANTHROPIC_VERSION` | `2023-06-01` | header added to every request |
| `UPSTREAM_TIMEOUT` | `180` | seconds |
| `FRIGATE_URL` | unset | e.g. `http://frigate:5000`; enables the profile line |
| `LAT`, `LON` | unset | enables the sunrise/sunset line |
| `FRIGATE_TZ` | `UTC` | zone of Frigate's "Activity started at" |
| `LOCAL_TZ` | `$TZ` or `UTC` | IANA zone of the property |

## Development

```sh
pip install pytest   # or: uv run --with pytest pytest -q
pytest -q
```

## Costs

Real numbers, Frigate 0.18.2, review alerts, preview frames:

| | tokens in / out | approx. |
|---|---|---|
| short event (3 frames), Haiku 5.5 | 3.5k / 1.2k | $0.001 |
| short event (3 frames), Sonnet 5.5 | 4k / 0.65k | $0.015 |
| 100 s event (many frames), Sonnet 5.5 | 11k / 0.8k | $0.03 |

## License

MIT
