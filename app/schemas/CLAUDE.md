# app/schemas

Pydantic models used at API boundaries (request bodies + response payloads).
Internal data structures NOT exposed over HTTP live in `app/models/`. SQLAlchemy
tables live in `app/db_models/`. Don't conflate.

## Files

| File | Purpose |
|---|---|
| `request_response_schemas.py` | Response wrappers (`SuccessfulResponseDataSchema`, `ErrorResponseSchema`) and every endpoint-specific response class. |
| `request_body_schemas.py` | Request bodies for `POST`/`PUT` endpoints. |
| `schemas.py` | Core data classes embedded inside responses (`UserGraphData`, `BrainstormRequestInstance`, etc.). |
| `graperank_schemas.py` | GrapeRank enums + parameter shape (`GrapeRankPresetParams`, preset enum). |
| `admin_sort.py` | Sorting enums for admin list endpoints (e.g. `pubkey \| times_calculated \| last_triggered \| last_updated`). |
| `error_codes.py` | Error enum returned inside `ErrorResponseSchema`. |

## Response wrapper convention

Every success response uses `SuccessfulResponseDataSchema`:

```python
class SuccessfulResponseDataSchema(BaseModel):
    code: int = 200
    data: SomePayload
    message: str | None = None
```

Each endpoint declares its own response class subclassing this, typed to the
right payload. Examples:

- `GetUserDataResponse` → `data: UserGraphData`
- `GetUserOverviewResponse` → `data: UserOverviewData`
- `AdminStatsResponse` → `data: AdminStats`

**Never return a bare dict** from a route handler. Always wrap.

Error responses use `ErrorResponseSchema` (raised as `HTTPException(detail=...)`
in services / route handlers).

## Reused payloads

A few payload classes appear in multiple routers — when changing them check ALL
the call sites:

- `BrainstormRequestInstance` (in `schemas.py`) — used by `/user/graperank`, `/admin/activity`, `/admin/users/{pubkey}/history`, `/admin/brainstormRequest`.
- `GrapeRankPresetParams` (in `graperank_schemas.py`) — used by `/user/graperank/preset/custom` and `/admin/graperank/preset/{id}`.
- `UserGraphData` (in `schemas.py`) — used by `/user/{pubkey}` and `/user/self`.

## Naming convention

- Request bodies: `<Verb><Noun>Body` (e.g. `CreateBrainstormRequestBody`, `SetGrapeRankPresetBody`, `SubmitNostrAuthChallengeBody`).
- Response classes: `<Verb><Noun>Response` (e.g. `GetUserDataResponse`, `PublishAssistantProfileResponse`).
- Payloads embedded in `data`: noun phrase (e.g. `UserGraphData`, `AdminStats`, `PaginatedUserConnections`).

Stick to these — handler signatures are a lot easier to scan.

## camelCase vs snake_case

- **Public API (request / response JSON)**: camelCase (e.g. `displayName`, `lastTriggered`, `attenuationFactor`).
- **Python / DB columns**: snake_case (e.g. `display_name`, `last_triggered`, `attenuation_factor`).

The `GrapeRankPreset` repo provides explicit `row_to_camel_dict` / `camel_dict_to_columns` converters because the JSONB column stores snake_case but the API uses camelCase — keep that conversion at the repo boundary.

For new endpoints, Pydantic field aliases (`Field(..., alias="someCamelKey")`) are the cleanest way to bridge the cases.

## Adding a new endpoint shape

1. Request body (if any) → `request_body_schemas.py`.
2. Inner payload class → `schemas.py` (or `<topic>_schemas.py` if it's complex enough for its own file).
3. Wrapped response class → `request_response_schemas.py`.
4. Route handler imports both, returns `MyResponse(data=MyPayload(...))`.
