# Divar Watchdog

Watches any number of Divar.ir search links and pushes new / changed listings to
Telegram, with a **Telegram admin panel** for everything else: add a link by
pasting it, change filters (max floor, rent, deposit, rooms, elevator …),
rename/enable/disable/delete searches, approve people who ask for access,
change the check interval, pause, or trigger a scan on demand.

**The whole idea in four steps:**

1. On [divar.ir](https://divar.ir), set your filters and draw/drag the map to the
   area you care about.
2. Copy the URL from the browser's address bar.
3. Paste it to your Telegram bot.
4. Every new listing in that area arrives in Telegram — photos, price, specs,
   location and a link.

> Unofficial project, not affiliated with Divar. It uses Divar's public web
> API at a deliberately slow, staggered pace — keep it that way.

## Setup

Requires Python 3.10+.

1. Install:

   ```
   git clone <this repo> divarMonitor && cd divarMonitor
   python3 -m venv venv && . venv/bin/activate
   pip install -r requirements.txt
   cp config.example.json config.json
   ```

2. Create a bot with [@BotFather](https://t.me/BotFather) and put the token in
   `config.json` (`telegram_bot_token`). Alternatively skip the `cp` and export
   `TELEGRAM_BOT_TOKEN` (see `.env.example`): environment variables fill in only
   the keys `config.json` doesn't have, and a `.env` file is not loaded
   automatically. `config.json` is created and kept up to date by the bot.
3. Put your numeric Telegram user id in `admin_ids`. Don't know it? Start the
   bot without any admin — it prints a one-time claim code:

   ```
   Open Telegram, message the bot and send:   /claim 3f9c1a7be2d04c86
   ```

   Whoever sends that code becomes the admin. (`/id` tells anyone their id.)
4. Run `python divar_watcher.py` and send `/admin`.

## Adding a search

Open divar.ir, set your filters and drag the map, then copy the address-bar URL
and **paste it to the bot**:

```
https://divar.ir/s/tehran/rent-apartment?bbox=...&credit=100000000-500000000&elevator=true&floor=0-&rent=10000000-30000000&rooms=یک,دو&multi_geo_polygon_encoded=...&sort=sort_date
```

The bot parses it, shows you what it understood, and offers:

* **🌱 Add (seed quietly)** — records what is already listed, then only alerts on
  what appears afterwards. This is what you normally want.
* **🔔 Add + alert on everything now** — fire alerts for the current results too.

That link is the source of truth: its filters, map polygon and sort order are
translated straight into the `api.divar.ir/v8/postlist/w/search` calls.

## Editing filters from Telegram

`/admin → Searches → <your search> → 🎛 Edit filters`

Each filter gets the right editor: ranges (rent, deposit, floor, area, building
age) have *set min* / *set max* plus quick presets, booleans (elevator, parking,
storage, balcony, photo) are yes/no, rooms is a multi-select, anything else is
free text. Money fields accept `40`, `40m` or `40000000` — all mean 40 million
Toman.

Changing "max floor" to 3 rewrites `floor=0-` into `floor=0-3` inside the saved
link, surgically, leaving the map polygon byte-for-byte intact. The next scan
uses it.

**➕ Add a filter** offers everything Divar supports for a search link, whether
or not the URL you pasted used it — so you can force `parking=true` on a link
that never had a parking filter. Anything outside the built-in list goes in
through **✏️ Custom (key=value)**, and the bot checks it against the live API
before confirming:

```
✅ Added parking=true.
✅ Divar accepted it — 119 listings match (was 153).
```

```
✅ Added furnished=true.
⚠️ Divar ignored this filter for this category, so it will not narrow anything.
```

That check works because Divar echoes back every filter it understood; the same
mechanism drives **🔢 Count matches** on the search screen. Verified as working
for apartment rentals: `rent`, `credit`, `price`, `size`, `rooms`, `floor`,
`building-age`, `parking`, `elevator`, `warehouse`, `balcony`, `has-photo`,
`has-video`.

## How the interval is split

`check_interval` is the period in which *every* search is visited once, and the
searches are spread evenly across it:

```
15 min interval, 3 links  ->  one scan every 5 min, each link revisited every 15 min
```

Only one search is scanned at a time, so Divar sees a steady trickle instead of
a burst. Adding, enabling or disabling a link re-spaces the rest on the next
tick — no restart. With many links the gap is floored at 20 s.

## Panel map

| Screen | What's there |
| --- | --- |
| **Searches** | per-search: enable/disable, rename, filters, show link, scan now, count matches, pages, update-alerts, re-seed, delete |
| **Settings** | check interval, max pages, update alerts, pause, proxy, advanced |
| **Advanced** | verify bumps, deep-check budget, page delay, search delay, retries, prune old state |
| **Recipients** | chats that receive listing alerts, plus pending access requests |
| **Requests** | people who sent `/ask`, one tap to accept or decline |
| **Admins** | who may use the panel |
| **Status** | per-search last/next scan, counters, errors, uptime |

Commands — admin: `/admin` `/status` `/add <url>` `/scan` `/pause` `/resume`
`/claim <code>` `/cancel`. Anyone: `/start` `/id` `/ask [message]`.

## Letting people in

Someone who is not on the recipient list sends **`/ask`** (optionally with a
note: `/ask دنبال آپارتمان اجاره‌ای هستم`). Every admin gets a card with
their name, username, id and note, and the bot **pins it in the admin chat** so
it cannot scroll away unanswered:

```
📥 Access request
👤 Sara (@sara_t)
🆔 777
💬 دنبال خونه می‌گردم
                      [ ✅ Accept ]  [ ❌ Decline ]
```

Accepting adds them to the recipients, unpins the card, rewrites it with the
verdict and who decided, and tells the requester. Pending requests also show up
as a badge on the main panel and in **📥 Access requests**, and they survive a
restart (they live in `config.json`). One pending request per person; after a
decline they must wait an hour to ask again.

## What an alert looks like

```
🆕 ۷۵ متر ۲ خواب نوساز، نورگیر

💰 ودیعه ۴۰۰م · اجاره ۲۵م

📐 ۷۵متر · 🛏 ۲خواب · 🏢 طبقه ۲

🚗 پارکینگ ✅ · 🛗 آسانسور ❌ · 📦 انباری ✅

📍 یوسف‌آباد, تهران · ⏰ ۱ ساعت پیش · 👤 مشاور املاک نمونه

🔎 my-search

🔗 https://divar.ir/v/AbCd1234
```

One block per kind of fact — title, prices, specs, where/when, which search,
link — with the photos attached. A change alert is the same with `♻️` plus a
block saying what moved:

```
♻️ ۷۵ متر ۲ خواب نوساز

💰 ودیعه ۴۰۰م · اجاره ۲۲م

💱 اجاره ۲۵م ⬅️ ۲۲م (🔻۱۲٪)

🚗 پارکینگ: ندارد ⬅️ دارد
🖼 عکس جدید (۳ ⬅️ ۵)

📐 ۷۵متر · 🛏 ۲خواب · 🏢 طبقه ۲

🚗 پارکینگ ✅ · 🛗 آسانسور ❌ · 📦 انباری ✅

📍 یوسف‌آباد, تهران · ⏰ ۲ ساعت پیش

🔎 my-search

🔗 https://divar.ir/v/AbCd1234
```

Parking, elevator and storage are always stated either way: Divar writes a
missing amenity as `"آسانسور ندارد"` rather than flagging it, so the watcher
normalises that into a ✅/❌ per amenity instead of leaving it out. The 👤 line
only appears for agency listings — on personal ads Divar reuses that field for
a timestamp, and the trailing ` در <محله>` it appends is dropped since ⏰ and 📍
already cover both.

## CLI

```
python divar_watcher.py                      # watch + admin panel
python divar_watcher.py --check-once         # one scan of every search, then exit
python divar_watcher.py --check-once --seed  # ... without sending alerts
python divar_watcher.py --add-url URL [--name NAME]
python divar_watcher.py --list-searches
python divar_watcher.py --prune-days 30      # forget listings unseen for 30 days
python divar_watcher.py --config c.json --state s.json
```

## What gets alerted

* **New** — a listing token never seen before.
* **Changed** — a tracked listing whose price, deposit, title, specs (size,
  rooms, floor, parking, elevator, storage, balcony) or photo set actually
  moved. The message says what changed.

**نردبان bumps never produce a message.** A bump only moves the listing's
`sort_date`; the listing itself is identical, so there is nothing to report.
When a bump does appear, the watcher opens that listing once and compares a
stored fingerprint (specs + photo signature) against the live page: if
something really moved behind the bump it alerts and says what, otherwise it
stays quiet. Turn that verification off (Settings → Advanced → *Verify bumps*)
to skip bumps without looking at all — cheaper, but a silent edit would slip
through. `max_deep_checks` caps how many listings a single scan may open this
way; listings recorded by a seed pick up their fingerprint the first time they
are checked.

Coordinates from the listing detail are re-checked against the link's map
polygon, so listings Divar returns outside your drawn area are dropped (logged,
not alerted). Rent/deposit ranges are also re-checked locally against **both**
ends of a convertible (قابل تبدیل) slider — Divar's search only filters on the
base end, so the scan fetches the wider set and alerts when *either* end has
its deposit in range **and** its rent in range; the alert shows the other end
as `🔄 قابل تبدیل: ...`. Listings are de-duplicated across overlapping searches through the
shared state file.

## Files

| File | Purpose |
| --- | --- |
| `divar_watcher.py` | entry point |
| `divarmon/` | `config`, `search_url` (link parsing + filter editing), `divar` (API), `geo`, `state`, `notify`, `monitor` (scheduler), `admin` (panel), `app` (wiring) |
| `config.example.json` | template for `config.json` |
| `config.json` | everything the panel writes — holds the bot token, git-ignored |
| `listings_state.json` | seen tokens + price history, git-ignored |
| `divar_watcher.log` | rotating, 10 MB × 3, git-ignored |

## Running as a service

```ini
[Unit]
Description=Divar Watchdog
After=network-online.target

[Service]
WorkingDirectory=/opt/divarMonitor
ExecStart=/opt/divarMonitor/venv/bin/python divar_watcher.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`SIGTERM` saves state and exits cleanly, so `systemctl restart` is safe.

## License

[GPL-3.0](LICENSE)
