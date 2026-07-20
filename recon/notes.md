# Phase 0 recon notes — pv.polycabmonitoring.com API

## Base URL
```
https://pv.polycabmonitoring.com/dist/server/api/CodeIgniter/index.php/Senergytec/web/v2/Inverterapi/<endpoint>
```
All requests are `POST` with a JSON body.

## Auth
- A JWT (HS256) is stored in `localStorage.token` by the SPA and sent as a raw `authorization`
  header (no `Bearer ` prefix).
- Decoded payload: `{iss, aud, iat, nbf, exp, data: {MemberAutoID: null}}`.
- Lifetime observed: **~360 days** (issued 2026-07-20, expires 2027-07-15). Long-lived enough
  that we don't need to script a login flow — extract the token once from the browser
  (DevTools → Application → Local Storage, or Network tab → any request → `authorization`
  header) and reuse it. Re-extract manually if it ever gets invalidated.
- No cookie-based session is used for API auth (the `cookie` header seen in requests only
  carries a `timezone` value and a browser-extension id, unrelated to auth).

## Request signing
Every request body includes a `sign` field alongside the real params, e.g.:
```json
{"sign": "zTWZ...==", "groupID": "516036", "date": "2026-07-20"}
```
This **is validated server-side** (confirmed: flipping one character of a valid `sign` while
keeping everything else identical causes a 404). Reverse-engineered from the frontend bundle
(function `ct`/`U$` in `umi.*.js`):

1. Drop params whose value is `""`, `null`, `undefined`, or boolean.
2. Sort remaining keys alphabetically, join as `key=value&key=value...` (arrays become the
   literal string `Array`).
3. Append `&05469137076236813460585715952089` (KEY1, reused as a literal trailing salt).
4. AES-256-CBC encrypt the resulting string, PKCS7 padding:
   - key = UTF-8 bytes of `"05469137076236813460585715952089"` (KEY1, 32 bytes)
   - iv = UTF-8 bytes of `"5161557162012237"` (KEY2, 16 bytes)
5. Base64-encode the ciphertext → this is `sign`.

Both keys are hardcoded in the frontend JS bundle, not session-specific. Verified byte-for-byte
against a real captured request (see `recon/api_client.py::sign`).

## Key identifiers for our system
- `MemberAutoID`: `1137916` (account: "Mr.Parthiban")
- `groupID` (plant/station id): `516036` (plant name "Parthiban", address "Mangadu")
- `GoodsID` (inverter serial number): `2620-119401326P` (model `PSIS-5K0`)

## Useful endpoints found
| Endpoint | Params | Gives us |
|---|---|---|
| `GroupList` | `{MemberAutoID, inputValue}` | Plant-level summary: `CurrPac`, `EToday`, `ETotal`, `Htotal`, `LastUpdate`, `InverterStatus` (green/yellow/red/gray counts) |
| `InverterDetail` | `{GoodsID}` | Basic per-inverter reading: `CurrPac`, `EToday`, `ETotal`, `Htotal`, `DataTime` |
| **`InverterDetailInfoNewone`** | `{GoodsID}` | **The main one to poll every 5 min.** Full electrical detail: `ACDCInfo.{Pac,Vac,Iac,Fac,Pdc,Vdc,Idc}` (AC/DC power/voltage/current/frequency, as arrays — per phase/string), `Tntc` (inverter temp °C), `EToday`, `ETotal`, `Htotal`, `DataTime`, firmware versions, wifi strength |
| `InverterDetailDayLine` | `{GoodsID, date}` | Intraday power curve, per-inverter: array of `{inTime: "HH:MM", pac}` at 5-min resolution. Came back **empty** (`pac: ""` all day) for our test date — seems unreliable/unpopulated, don't rely on this one. |
| **`getAllPacDay_v1`** | `{MemberAutoID, date}` | **Better source for the "power over the day" chart.** Member-scoped (fine for single-inverter accounts) intraday curve: array of `{inTime: "HH:MM:SS", pac}` at 5-min resolution, values already in **kW**. Only covers sunrise-to-sunset (e.g. 06:00–18:40), not full 24h. Confirmed populated with real data. |
| `groupAreaCurve` | `{groupID, date}` | Monthly production curve: 31-day array of `{day, Production, powerToGrid, powerFromGrid, ..., Consumption}` — daily granularity, for history/backfill. Superseded by `getAllPacMonth` below for the simple case (this one carries a lot of always-zero hybrid/battery fields for a non-hybrid system). |
| **`getAllPacMonth`** | `{MemberAutoID, date:"YYYY-MM"}` | **Best source for the daily-yield history chart.** Array of `{inDate: "YYYY-MM-DD", pac}`, one row per day of the month, already in **kWh**. |
| **`getAllPacYear`** | `{MemberAutoID, Year:"YYYY"}` | Array of `{inMonth: "MM", pac}`, one row per month, already in **kWh**. For a "this year" rollup view. |
| `GetMemberData` | `{}` (uses token) | Account info: email, currency, timezone list |

Sample raw responses saved in `recon/sample_responses/`.

## Additional endpoints checked (comprehensiveness pass)
| Endpoint | Params tried | Result |
|---|---|---|
| `getPlantPvierrorRealtime` | `{GoodsID}`, `{MemberAutoID}` | Both `{"status": false, "message": "no params"}` — correct param name not found yet. Not blocking, see below. |
| `goodsTypeMapDetail` | `{GoodsID}`, `{groupID}` | `{"msg": "参数异常"}` ("parameter exception") both times — wrong shape, and likely just UI icon-mapping metadata anyway, not fault codes. Deprioritized. |
| `memberGoodsTypeMap` | `{MemberAutoID}` | `[]` — correct shape probably, but empty/not useful for fault codes. |
| `GroupDetailYearLineSum` | `{groupID, date}` | 12-row array `{inMonth, pac}` — yearly rollup by month. Same idea as `groupAreaCurve` but year-granularity. Low priority (month-level `groupAreaCurve` already covers the dashboard's date-range needs), but cheap to add later for a "this year" view. |
| `getAllPacYear` | `{MemberAutoID, date}` | Same shape as above, member-scoped equivalent. |

## Open questions / not yet done
- **Fault/error codes**: still unresolved — tried both `getPvierrorRealtime` and
  `getPlantPvierrorRealtime` with several param name guesses, no luck. Not blocking —
  `GroupList.InverterStatus` (green/yellow/red/gray counts) and the `Light`/`type` numeric codes
  already give a coarse health signal. Revisit only if we actually need decoded fault text.
- **`InverterStatus` color meaning — resolved** (confirmed by user, matches Polycab's own UI
  legend): `Green = normal`, `yellow = standby`, `red = abnormal`, `gray = offline`. Use this
  directly for the dashboard's health/status display instead of chasing the raw numeric
  `Light`/`type` codes.
- **LAN datalogger reachability (Plan B fallback)**: not checked yet — still need to look at the
  router's DHCP client list for a Solarman/LSW-named device, per plan.md Phase 0 step 4.
- **Unit inconsistency**: `GoodsKWP` appears as `"5"` (string, kW) in `GroupList` but `5000`
  (int, W) in `GroupDetail`/`MemberMonitor`. `EToday`/`ETotal` are in **Wh** everywhere (e.g.
  `10270` = 10.27 kWh), matching the UI's kWh display after /1000.
- **Real reporting interval — inconclusive, needs a daytime retest.** Polled
  `InverterDetailInfoNewone` twice, 65s apart, at night: `DataTime` was identical both times
  (inverter had gone idle — `ESP32Version.Status: "Offline"`, `WifiStrength: 0`). So the
  timestamp freezes when the inverter isn't producing, rather than ticking on a fixed clock —
  this test needs to be redone during active daylight production (e.g. tomorrow mid-morning,
  poll twice ~1-2 min apart) to see the actual hardware→cloud sync cadence. 5-minute collector
  polling is very likely still fine either way, just not yet empirically confirmed.

## Acceptance check (Phase 0)
`recon/api_client.py` is a working Python snippet that authenticates (via extracted token) and
returns current inverter data as JSON — run with `POLYCAB_TOKEN` env var set. Confirmed working
against the live account.
