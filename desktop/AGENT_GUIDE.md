# Desktop MCP — mandatory guide for the agent

Read all of it before your first action on the desktop. The server sends a
summary in its connection instructions and the full text through the
`usage_guide()` tool. Where this guide disagrees with one of your habits, the
guide wins.

## 1. Most important: the user must SEE you work

The user watches the screen while you work. The **orange "Claude" ghost
cursor** and the **monitor border** are the only way they know where you are
and what you are about to touch. An agent working without a visible cursor is
working badly, even if the result is right.

How it works:

- **Both are mandatory.** Any call to any desktop tool lights the border and
  the cursor on the monitor you are working on, and they stay on while the
  tool runs. They fade **together, by themselves**, a few seconds after your
  last call (`border.working_linger_s` + `idle_hide_s`, ~23 s). They cannot
  be hidden: `ghost_cursor("hide")` and `screen_border("hide")` are refused,
  and `ghost.enabled` / `border.enabled` in the config are ignored.
- The cursor only **moves** when a tool gets a target: `click`, `invoke`,
  `set_value`, `select`, `type_text`, `key`, `scroll`, `drag`, `read_value`,
  `focus_window`, `launch_app`, `move_window`, `callout` and `ghost_cursor`.
  `screenshot`, `ui_tree`, `find_element`, `list_windows`, `wait_for` and
  `status` leave it where it was: if you only look and read, the user cannot
  tell what you are looking at.
- It is invisible in captures (`WDA_EXCLUDEFROMCAPTURE`). Not seeing it in
  your `screenshot` does NOT mean it is off: `screenshot` draws it on top
  when `show_cursor=true` (the default), and `status()` reports its state.

Rules:

1. **When a task starts**, call `ghost_cursor(action="move_to", x, y)` on the
   window or area you will work in, even if you are only going to read.
2. **While you read or analyse** something (a table, a message, a field),
   point at it with `ghost_cursor("move_to")` or outline it with
   `ghost_cursor(action="highlight", rect=[l,t,r,b])`.
3. **Act by element or by point**, never blind: `click(element=…)` or
   `click(x, y)` make the cursor arrive before acting. Do not use `key` /
   `type_text` without `window` or `element` when you can give one.
4. If the user says they do not see the border or the cursor, check
   `status()` (`overlay.state`, `overlay.error`, `all_excluded_from_capture`)
   and tell them; do not keep working blind.
5. To **explain or ask** something on screen, use `callout` (section 5), not
   only text in the chat.

## 2. Install and check

**One server**: `server_new.py`. On servers it runs as a service and only
has the ops tools. On a user's PC it runs with `--desktop` inside their
session and adds the screen tools of this guide. There the deployment tools
(`full_deploy`, `git_pull`, `django_migrate`, `django_collectstatic`,
`pip_install`, `iis_start/stop/restart`) are **not** offered: on a PC they
would touch the local checkout or database by mistake. Deploy through the
server's own instance, never this one.

It runs as a **scheduled task at logon** (not a service: session 0 has no
desktop). From a normal PowerShell, no admin needed:

```
powershell -ExecutionPolicy Bypass -File install_service.ps1 -Desktop
```

- Creates `desktop\.venv` (needs an installed Python ≥ 3.11), installs
  `requirements-desktop.txt`, runs the tests, generates the key in the user
  variable `DESKTOP_MCP_API_KEY`, registers the task `Desktop-MCP` (which runs
  `server_new.py --desktop`), starts it and checks `/health`. Re-running it
  repairs everything. `-DesktopRemove` removes the task. An old task still
  pointing at `desktop\desktop_server.py` keeps working: that file starts the
  unified server.
- Listens on `127.0.0.1:8011` only. Register it in Claude Code (the installer
  prints the command; it does not run it):
  `claude mcp add-json --scope project desktop-mcp '{"type":"http","url":"http://127.0.0.1:8011/mcp","headersHelper":"powershell -NoProfile -ExecutionPolicy Bypass -File <path>/desktop/mcp_headers.ps1"}'`
- **Never** put the key in `.mcp.json` nor use
  `--header 'Bearer ${DESKTOP_MCP_API_KEY}'` (it can expand empty → 401).
  `headersHelper` reads it from the registry on every connect.
- `/health` says `"desktop": true` when the screen tools are loaded, plus
  `overlay_state` and `ghost_visible`, without lighting anything.
- If the tools do not answer: `status()`; the log is
  `%LOCALAPPDATA%\desktop-mcp\server.log`. Installing, reinstalling or
  restarting the task is the user's decision: ask, do not do it on your own.

## 3. Recommended workflow

1. `status()` once: check you are not paused and the overlay has no error.
2. `list_windows()` → `focus_window(window)` (the cursor goes to the window).
3. `ui_tree(window)` or `find_element(window, query)` for element ids. Prefer
   ids over coordinates: they do not depend on the resolution.
4. Act with `invoke` / `set_value` / `select` / `click(element=…)`. Use
   coordinates only when the app exposes no elements (drawing, canvases):
   then `screenshot` and `click(x, y, capture_id=…)` with that capture's id.
5. Verify with `read_value`, `wait_for` or `ui_tree`, not by guessing from
   the image. `screenshot` with `region` and `scale` to save tokens.
6. To see what happened during a quick action: `recent_frames=3..9`.

Element ids expire when the element disappears or the server restarts: on
"Unknown element id", ask `ui_tree` again.

## 4. Mechanisms: one cursor in Windows

Each action uses the first mechanism that works and reports it in
`mechanism` and `cursor_moved`:

1. UI Automation — does not move the real cursor nor steal focus (preferred).
2. Win32 messages to the window — does not move the real cursor.
3. SendInput (drawing, dragging, apps that ignore 1 and 2) — moves the
   user's real cursor and puts it back.

Leave `mechanism="auto"`. Force `"sendinput"` only when the others have no
effect, and tell the user: their mouse moves. `freeze_user_mouse` only with
their permission.

## 5. Comment bubbles (`callout`)

For guided tours, explanations and questions to the user:

- `callout(text, rect=[l,t,r,b] | x,y | element=id, title, step="2/5")` shows
  a bubble beside what you point at, with the area outlined and the ghost
  cursor on it. **OK** button.
- `ask=True` adds an answer field; the answer comes back in `answer`.
- `wait=True` (default) waits: `accepted` | `answered` | `dismissed` |
  `replaced` | `stopped` | `pending` (at `timeout`; continue with
  `callout_wait(id)` or remove it with `callout_close(id)`).
- Only the user's physical click and keys count. Do not try to read, press
  or type in the bubble: the policy always blocks it.
- One bubble at a time; a new one replaces the previous one.

## 6. Stopping, safety and limits

- The user stops you with the on-screen **Stop** button, the hotkey
  Ctrl+Alt+Shift+F12, `pause()` or a chat message. If a tool answers that you
  are paused, **stop and ask**; do not call `resume()` on your own.
- The user moving their mouse or typing does not pause you: do not compete
  with them. If they are using the same window, wait or ask.
- Password fields are never typed into or read, nor processes / titles in
  the deny lists (password managers, UAC, banking…). A `PolicyError` is
  final: do not look for another way.
- Apps running as administrator do not accept control (UIPI): tell the user.
- Everything is logged in `%LOCALAPPDATA%\desktop-mcp\audit.jsonl`
  (`audit_tail(n)`).
- Irreversible actions (send, delete, confirm, pay) still need the user's
  approval in the chat, as anywhere else.
