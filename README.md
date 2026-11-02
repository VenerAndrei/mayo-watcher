# Mayo Terrace tour watcher

Checks the Kewpie Mayo Terrace (Tokyo) calendar every 15 minutes and sends a push notification to your phone when an English tour has places for 2 people. **It only reads and notifies. It never books.** You book by hand from the link in the alert.

- **Dates:** 30 Nov – 4 Dec 2026
- **Tours:** rows with "English" in the title (`--any-tour` adds the Japanese ones)
- **Stops:** by itself after 4 Dec 2026 (Japan time); the workflow then disables its own schedule

An alert looks like this:

```
Mayo Terrace English tour: 2 places — Thu 3 Dec 14:00 (3 left)
```

## Setup

### 1. Install ntfy on your iPhone

1. Install **ntfy** from the App Store and allow notifications.
2. Tap **+** and subscribe to a topic name. Leave the server as `ntfy.sh`.

Anyone who knows a topic name can read it or post to it, so a long random name is safest.

### 2. Create the repo and push

```sh
git init -b main
git add .
git commit -m "Add Mayo Terrace tour watcher"
gh repo create mayo-watcher --public --source . --push
```

The repo is public because Actions minutes are then free and unlimited. The code contains no secrets, but run logs are public and show the tour statuses.

### 3. Add the secret

```sh
gh secret set NTFY_TOPIC
```

Paste the topic name when prompted.

### 4. Run it once by hand

English tours may have no places, so this test includes Japanese tours and asks for 1 place. A real alert should reach your phone if any tour has room:

```sh
gh workflow run mayo.yml -f people=1 -f any_tour=true
gh run watch
```

To test the normal watch instead, run `gh workflow run mayo.yml` with no inputs; it stays silent unless an English tour has places. Each run's log lists the status of every watched slot, so you can check the site is being read correctly.

The schedule then runs by itself, with your laptop off. GitHub can start scheduled runs some minutes late.

## Running locally

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # on macOS/Linux: .venv/bin/python
.venv/Scripts/python -m playwright install chromium
```

Check the calendar is being read, in a visible browser:

```sh
python mayo_watch.py --once --show
```

Confirm a hit is detected when any tour has room:

```sh
python mayo_watch.py --once --people 1 --any-tour
```

| Flag | Meaning |
|---|---|
| `--once` | Check once and exit. Without it, keeps checking. |
| `--interval` | Seconds between checks when looping. Default: 900. |
| `--dates` | Comma-separated `YYYY-MM-DD`. Default: 30 Nov – 4 Dec 2026. |
| `--people` | Places needed. Default: 2. |
| `--any-tour` | Also include Japanese tours. |
| `--state-file` | Where to keep alert history. Default: `state.json`. |
| `--dry-run` | Print what would be alerted. Sends nothing and saves nothing. |
| `--show` | Use a visible browser, for debugging. |

If `NTFY_TOPIC` isn't set locally, the script behaves as `--dry-run`. On GitHub a missing secret fails the run.

Offline tests: `python -m unittest test_mayo_watch`

## Alert rules

- A hit is a watched tour, on a watched date, with `残り N 人` where N is at least the places needed. `残り0人`, `Not Open`, `受付前`, `満席`, `開催なし`, `休館日`, `休業日` and empty cells are not bookable.
- You get an alert when a hit newly appears, or is still there 2 hours after its last alert. Every current hit goes in the same message.
- Tapping an alert opens the first slot's booking link, and up to 3 slots also get a button.
- A check that fails 4 times in a row (timeout, selectors not found, zero slots parsed) sends one "Mayo watcher broken" alert. A later good check re-arms it, without sending a message.

A failed check doesn't turn the GitHub run red, because that would email you every 15 minutes during an outage. Look for a warning on the run instead. A run does go red if an alert can't be delivered or the secret is missing.

## Stopping early

```sh
gh workflow disable mayo.yml
```
