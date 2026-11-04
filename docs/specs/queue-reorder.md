# Spec: reordering the SABnzbd queue from Downloads

Status: built on 2026-11-04.

One change from the plan below: there's no stored full-queue order (`info.live_order`). A drop sends both neighbours, and the service asks SABnzbd for their present order by `nzo_ids` (about 5 ms) before choosing the target. That copes with two quick drags in a row and with a neighbour that has just finished. See `plan_move` in `services/arr.py`.

## Why

The Downloads page shows TV and films from SABnzbd, with runs of other jobs grouped into "Other items" between them. TV and films often sit behind 1,000+ other jobs. Marc wants to drag a job to a new place in the queue from Binge Stats, without opening SABnzbd.

## The rule change

DESIGN.md section 1 says Sonarr, Radarr and SABnzbd are strictly read-only (Marc, 2026-10-31). This is a narrow exception (Marc, 2026-11-04):

- **SABnzbd only.** Sonarr and Radarr stay GET-only, unchanged.
- **Exactly one write call:** `mode=switch`, which moves one job. No delete, pause, resume, priority, rename or category call, now or as a side effect.
- **Off by default.** A new setting, `sabnzbd.allow_moves` (default `false`), toggled in Settings, SABnzbd card: "Allow moving jobs in the queue from Downloads". While it's off, no control renders and the endpoints return 403.
- **Admin only.** Viewer logins never see the controls, and the routes aren't in the `me` blueprint. In the REST API, write-scope tokens only.

Update these to match:
- DESIGN.md section 1, the Sonarr/Radarr/SABnzbd row;
- SPEC.md, the arr section (currently "limited to the version, server_stats, history and queue modes");
- CLAUDE.md, the `bingestats/arr/` line. Proposed wording: "GET only, except SabClient.move (mode=switch), behind sabnzbd.allow_moves. Never add another write call."

## SABnzbd behaviour (checked on Marc's server, SABnzbd 5.1.3, 1,368 jobs)

These were checked by moving a real job deep in the queue and putting it back. Record them in `docs/verification.md`.

- **The call.** `GET /api?mode=switch&value=<nzo_id>&value2=<target>` answers `{"result": {"position": N, "priority": P}}`. It took well under a second.
- **`value2` can be another job's `nzo_id`**, as well as an index. **Always send an nzo_id.** Indexes drift by the second as jobs at the top finish: indexes moved by one between two reads during the test.
- **Where the job lands.** SABnzbd removes the job, then inserts it at the target's index as it stood before the removal:
  - **Moving up**, it lands just **before** the target.
  - **Moving down**, it lands just **after** the target.
- **Priority follows the target.** Moving a Normal job onto a High job made it High and put it at position 0. Moving it back onto a Normal job made it Normal again. So SABnzbd's rule that High stays above Normal never makes a move bounce back, and Binge Stats never calls the priority API itself.
- **The response has the truth.** `position` and `priority` say where the job really ended up. Use them, not what the UI asked for.

## Client: `bingestats/arr/client.py`

- Add `SabClient.move(nzo_id: str, target_nzo_id: str) -> dict`. It returns `{"position": int, "priority": int}`.
- It is the only method that sends a mode outside `READ_MODES`. Use a separate constant, `WRITE_MODES = {"switch"}`, and keep `get()` refusing anything that isn't a read mode.
- Validate both ids against `^[A-Za-z0-9_-]{6,64}$` before sending. SABnzbd 5 ids are UUIDs (`61fb3d9a-5559-…`); older versions use `SABnzbd_nzo_…`.
- `move()` checks the setting itself, through a flag passed in by the service, so a stray caller can't bypass the switch.
- Timeout: 15 s.

## Service: `bingestats/services/arr.py`

`move_job(conn, nzo_id, before=None, after=None) -> dict`:
- Exactly one of `before` and `after` is given. Each is a **job** nzo_id, never a group:
  - `before=X` means "put it just ahead of X";
  - `after=X` means "put it just behind X".
- **Pick the target from the job's direction**, using the last full read's order. This handles SABnzbd's up/down asymmetry:
  - **Moving up:** to land before X, the target is X. To land after X, the target is the job that follows X.
  - **Moving down:** to land after X, the target is X. To land before X, the target is the job that precedes X.
  - If X is the job itself, or the move is a no-op, return without calling SABnzbd.
- **Refusals:**
  - Raise `ValueError` if the setting is off, SABnzbd isn't configured, either id isn't in the last full read, or both or neither of `before`/`after` are given.
  - Raise `ArrError` if SABnzbd refuses or can't be reached.
- **After a successful move, queue a `downloads_refresh` job** (see the worker section).
- Return `{"position", "priority", "priority_name"}`. `priority_name` maps -1 Low, 0 Normal, 1 High, 2 Force.

The service needs the full queue order, not just the rows on show. The watcher keeps it in memory, so store it with the snapshot: add `info.live_order` to `arr_status`. That's a list of `[nzo_id, index]` for every job in the last full read. About 30 KB for 1,400 jobs; only the worker writes it.

## Worker: `bingestats/worker/downloads.py` and `jobs_runner.py`

- **Group edges.** Group rows from `group_rows` also carry `first_id` and `last_id`, the nzo_ids of the first and last job in the run. The UI needs them for drops before or after a group.
- **New job kind `downloads_refresh`** ("Re-read the SABnzbd queue" in `services/jobs.py`). Its handler sets a `threading.Event` that the DownloadWatcher waits on as well as its stop event. The watcher's next tick then does a full read straight away: set `st["full_at"] = 0` and wake it.
- **De-duplicate.** Use `jobs.ensure`, as `arr_sync` does, so ten quick drags make one refresh, not ten.
- **Latency.** Jobs are picked up within 2 s. On a slow SABnzbd the full read can still take up to 40 s, which is why the UI is optimistic.

## Routes

**UI:** `POST /downloads/move`, in `routes_ui.py`, with CSRF.
- Form fields: `nzo_id`, plus `before` or `after`.
- Returns JSON `{ok, position, priority_name}`, or `{ok: false, error}` with 400 or 502.
- Admin only, through the normal gate.

**REST API:** `POST /api/v1/downloads/<nzo_id>/move` with JSON `{"before": id}` or `{"after": id}`.
- Requires `require("write")`.
- Same service call, same response shape as the API's other write endpoints. Document it in the API section of SPEC.md.

## UI: `partials/downloads_live.html` and `pages/downloads.html`

- **Library.** Use SortableJS for dragging. Pin the latest stable 1.15.x in `bingestats/static/vendor/VERSIONS`, with sha256, fetched by `scripts/fetch-assets.sh`. No CDN at runtime. Load it only on the Downloads page, and only when moves are allowed.
- **Handle.** Each TV/film card gets a grip handle on its left edge, using the `grip` icon from the icon macros (add one if there isn't one). Only the handle starts a drag (`handle: '.drag-handle'`), so scrolling on a phone isn't hijacked. Use a short touch delay (`delay: 150, delayOnTouchOnly: true`).
- **Groups.** "Other items" rows are not draggable (`filter`), but they're valid drop neighbours. Dropping just above a group means "before its `first_id`", and just below means "after its `last_id`". You can't drop into the middle of a group. Each group is one block on the page.
- **What to send:**
  - If the row now below the dropped card is a job, send `before=<its nzo_id>`.
  - If it's a group, send `before=<its first_id>`.
  - At the very bottom, send `after=<last row's id, or its last_id if it's a group>`.
- **Pause polling during a drag.** The page polls every 2 s. Change the trigger to `every 2s [!document.hidden && !window.bsDragging]`. Set `window.bsDragging` on `onStart`, and clear it once the move request has resolved, not on drop, so a stale poll can't snap the card back.
- **Optimistic.** The card stays where it was dropped, with a subtle "Moving…" state (reduced opacity and a spinner in the handle area) until the request returns.
  - **On success**, the card shows its new ETA from the next poll. If its priority changed, it shows a small toast: "Now High priority, to sit there". The full-read refresh then confirms the order.
  - **On failure**, it reverts to its original place (call Sortable's `sort()` with the saved order) and toasts the error.
- **Quick moves.** Keep a per-card menu (the kebab icon) with **Move to top** (before the first job in the queue) and **Move to bottom** (after the last).
- **Header button: TV & films first.** It moves every job on show to the front, keeping their order. The service makes one `switch` call per job, last job first, each "before the current first job". It refuses if there are more than 100 jobs on show. Confirm with a small inline "Move N jobs to the front?" before it runs.
- **Phone width.** The handle and kebab must stay tappable (44 px targets) at 375 px wide. Check both themes.
- **Empty state.** No change: no jobs on show means no handles.

## Tests (`tests/test_arr.py`)

- **`SabClient`:**
  - `get()` still refuses `switch` and anything else outside `READ_MODES`;
  - `move()` sends exactly `mode=switch&value=..&value2=..`;
  - `move()` rejects bad ids.
- **`move_job` target choice.** Cover moving up and down, before and after, using an order list: up+before → X, up+after → next(X), down+after → X, down+before → prev(X). Also: a no-op makes no call, and so does X being the job itself.
- **Refusals:** the setting off (403 from the route, ValueError from the service), an unknown id, and both or neither of `before`/`after`.
- **Refresh job.** A move queues one `downloads_refresh` job, and two moves still leave one queued. The handler makes the watcher's next tick a full read.
- **Group edges.** `group_rows` sets `first_id` and `last_id`.
- **Routes:**
  - a viewer can't POST `/downloads/move`;
  - a read-only API token gets 403;
  - a write token works;
  - CSRF is enforced.
- **Page:**
  - with moves allowed, the page has the drag handles, the menu and the SortableJS script;
  - with moves off, none of them render.
- **TV & films first:**
  - makes N calls in the right order;
  - refuses over 100 jobs.

## Verify against live (write findings to `docs/verification.md`)

- Drag a TV job above a big "Other items" group, and check SABnzbd's own UI shows it there.
- Drag it back down, and check where it lands relative to the neighbour you dropped it next to.
- Drop a Normal job above a High one, and check the toast says High and SABnzbd agrees.
- Try it on a phone (the handle, touch delay, scrolling while not dragging).
- Turn the setting off, and check that the controls go and `POST /downloads/move` answers 403.

## Not in scope

- Moving "Other items" groups: that would be one call per hidden job, potentially 1,000+.
- Pausing, resuming, deleting or changing the category or priority of jobs directly.
- Anything that writes to Sonarr or Radarr.
- Reordering from Home Assistant.
