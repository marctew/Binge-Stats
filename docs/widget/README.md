# Binge Pins: iPhone home screen widget

`binge-pins.js` is a [Scriptable](https://scriptable.app) script. It shows your next pinned episode (with its poster on the medium and large sizes), plus how many pinned shows are **Ready**, **Downloading**, **Stalled** and **Missing**. Tapping it opens the calendar.

1. **Get a token:** in Binge Stats go to Settings → API tokens and create one. Read-only is enough.
2. **Add the script:** install Scriptable from the App Store, make a new script, paste in `binge-pins.js`, and name it "Binge Pins".
3. **Add the widget:** long-press the home screen → **+** → Scriptable, and pick a size.
4. **Point it at Binge:** long-press the widget → Edit Widget. Set Script to **Binge Pins** and Parameter to:

   ```
   https://binge.example.com|YOUR_TOKEN
   ```

   Add `|USER_ID` on the end to show someone else's pins. The id is in the address bar on their Users page.

The phone has to reach Binge Stats: on the LAN, over Tailscale, or through your tunnel. The token lives in the widget's parameter rather than the script, so the script can be shared and one copy can drive several widgets.

**If it shows an error,** the widget says which kind:
- **Unreachable:** the phone couldn't connect at all.
- **Said no:** Binge answered with a refusal, e.g. an unknown token.
- **An HTML page:** the address hit a login page or a proxy instead of Binge Stats.

For more detail, open the script in Scriptable and press ▶ after filling in `TEST_URL` and `TEST_TOKEN` at the top. It prints what the server sent back.

The wording comes from `/api/v1/pins/widget`, so it can change server-side without reinstalling anything. iOS decides when widgets refresh (the script asks for every 15 minutes); if the data is older than 45 minutes the widget says "stale".
