# Read Me

Forward an email to `you+readme@gmail.com`, subscribe to newsletters with that address, or label an email **ReadMe** in Gmail, and within the hour it shows up in the Read Me app and your podcast feed, read by a natural voice. Nothing is rewritten: Claude only acts as an editor that cuts the clutter (footers, ads, "view in browser"), so you hear and read the author's own words. The reader keeps the links, photos and formatting, and highlights each paragraph as it's read so you can follow along.

```
Gmail ─▶ Claude Haiku (marks the clutter to cut) ─▶ Google voice, one clip per paragraph ─▶ Read Me app + podcast feed
```

It runs on GitHub Actions every hour. There's no server to keep running, and everything fits in free tiers except Claude, which costs well under a cent per email.

## Make your own copy (keep it private)

Everyone runs their own copy with their own Gmail, Claude key and Google account; nothing is shared with anyone else.

1. Click **Use this template → Create a new repository** (or fork it), and choose **Private**.
2. Follow the setup below.

**Keep your copy private.** GitHub shows a public repository's run logs to everyone, and those logs include your app's private link (and the subject lines of your emails). Anyone with that link can read your emails.

## What it costs

| Piece | Cost |
|---|---|
| GitHub Actions (runs the script hourly) | Free (uses about a third of the 2,000 free minutes a month for private repos) |
| Google Text-to-Speech, Chirp 3 HD voice | Free for 1 million characters a month: roughly 40 long newsletters read in full, many more short emails. The script stops at 950,000 so it never goes over. |
| Google Cloud Storage (MP3s and feed) | Free under 5 GB. Episodes are deleted after 30 days. |
| Claude Haiku 4.5 | Well under 1¢ per email (it only reads an outline and names what to cut) |

## Setup (one time, about 30 minutes)

### 1. Gmail

1. Turn on **2-Step Verification** for your Google account if it isn't already on (myaccount.google.com → Security).
2. Create an **app password**: go to myaccount.google.com/apppasswords, name it "Read Me", and copy the 16-letter password it shows you.
3. In Gmail, create a label called **ReadMe**.
4. To send emails to it quickly, set up a forwarding address:
   - Gmail → Settings → Filters → Create a new filter → **To:** `yourname+readme@gmail.com` (your own address with `+readme` added).
   - Choose **Apply the label: ReadMe** (and **Skip the Inbox** if you like).
   - Anything sent or forwarded to `yourname+readme@gmail.com` gets read, even from your own inbox. (Gmail doesn't run filters on mail you send yourself, so the app looks for that address directly; the filter is what keeps newsletters you subscribe with it out of your inbox.)
5. Optional: make filters that apply **ReadMe** to newsletters automatically (filter by the newsletter's From address).

You can also label any email by hand in the Gmail app: open it, tap **⋮ → Label**, and pick **ReadMe**.

### 2. Claude API key

This is billed separately from a Claude.ai subscription.

1. Sign in at console.anthropic.com.
2. **Billing** → add $5 of credit, which lasts a long time at these prices.
3. **API Keys** → Create key, then copy it.

### 3. Google Cloud (voice and storage)

1. Go to console.cloud.google.com and **create a project** (for example "read-me").
2. **Billing**: link a billing account. Google requires a card even for free-tier use. For peace of mind, add a budget alert: Billing → Budgets & alerts → create a $1 budget.
3. **Turn on the voice API**: APIs & Services → Library → search **Cloud Text-to-Speech API** → Enable.
4. **Create the bucket**: Cloud Storage → Buckets → Create.
   - Name: anything unique, for example `readme-yourname-2026`.
   - Location type **Region**, and pick `us-central1`, `us-east1` or `us-west1` (these are the free-tier regions).
   - Storage class **Standard**. Access control **Uniform**.
   - **Uncheck** "Enforce public access prevention on this bucket".
5. **Let podcast apps fetch files**: open the bucket → Permissions → Grant access.
   - Principal: `allUsers`
   - Role: **Storage Legacy Object Reader**

   This lets someone who has an exact file link download that file, but nobody can list what's in the bucket. Your files sit under a random folder name only you know.
6. **Create a robot account for the script**: IAM & Admin → Service Accounts → Create service account named "read-me". Skip the optional role steps.
7. Give that robot access to the bucket: bucket → Permissions → Grant access → paste the service account's email (it looks like `read-me@your-project.iam.gserviceaccount.com`) → role **Storage Object Admin**.
8. Make its key: Service Accounts → read-me → **Keys** → Add key → Create new key → **JSON**. A file downloads. Open it in a text editor and copy everything. Keep this file private.

### 4. GitHub settings

In this repo: **Settings → Secrets and variables → Actions**.

On the **Secrets** tab, add:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-letter app password from step 1 |
| `ANTHROPIC_API_KEY` | the Claude key from step 2 |
| `GCP_SERVICE_ACCOUNT_JSON` | the whole contents of the JSON key file from step 3 |

On the **Variables** tab, add:

| Name | Value |
|---|---|
| `GCS_BUCKET` | your bucket name from step 3 |
| `FEED_SECRET` | a long random string of letters and numbers, 20+ characters (a password generator works). This becomes the private folder name in your links. |

Optional variables:

| Name | Default | What it does |
|---|---|---|
| `GMAIL_LABEL` | `ReadMe` | which label to read |
| `FEED_TITLE` | `Read Me` | the app and podcast name |
| `TTS_VOICE` | `en-US-Chirp3-HD-Charon` | the voice. Other Chirp 3 HD voices: `-Aoede`, `-Puck`, `-Kore`, `-Fenrir`, `-Leda`. Keep the `Chirp3-HD` part to stay in the free tier. |
| `TTS_SPEAKING_RATE` | `1.0` | speaking speed, e.g. `1.15`. Podcast apps also have a speed control. |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | which Claude model decides what to cut |
| `MONTHLY_CHAR_LIMIT` | `950000` | stop making audio after this many characters a month |
| `KEEP_DAYS` | `30` | delete episodes older than this |
| `LOOKBACK_DAYS` | `7` | only look at emails received in the last this-many days |
| `ANTHROPIC_WORKSPACE_ID` | (none) | only needed if your Claude key isn't tied to a workspace; the `wrkspc_…` ID from console.anthropic.com |
| `READING_ADDRESS` | your address with `+readme` | emails sent to this address are read even without the label |

### 5. First run

1. Label an email **ReadMe**.
2. Go to the **Actions** tab → **Read my emails** → **Run workflow**.
3. Open the finished run. The last lines of the log print two links:
   - **Player page**: open it in Chrome on your phone and tap **Install** (top right of the page, or ⋮ → *Install app*). It lands in your app drawer and opens full screen like any other app, and still shows the last list when you're out of signal. Tap an email to read it, like an inbox. Swipe an email right to remove it (there's an Undo for 5 seconds): it disappears on your phone right away, and once the voice function is set up the next hourly run deletes it and its audio for good. Tap **▶ Listen** at the top of the email to hear it: the player comes up at the bottom and the paragraph being read is highlighted as the page scrolls along (the **Aa** button changes the text size).
   - **Podcast feed**: in your podcast app, choose "add show by URL" and paste it. In Apple Podcasts on iPhone it's Library → ⋯ → Follow a Show by URL. Pocket Casts, Overcast and AntennaPod have the same option.

After that it runs by itself every hour. If an email can't be read, GitHub emails you about the failed run. That email is retried up to 3 times.

## Always voice, or voice when you tap Listen

Every email is ready to **read** within the hour. Audio is made two ways:

- **Always voice:** senders you switch on in the app (**⋮ → Settings**) get audio as soon as their emails arrive, so it's ready offline and in your podcast app. Nothing is switched on at first; to start with some, set the `ALWAYS_VOICE` variable to a comma-separated list of sender addresses.
- **On demand:** everything else is voiced when you tap **Listen** in the app: it takes a few seconds, and the audio is saved so replays are instant. Those emails join the podcast feed on the next hourly run.

On-demand audio and the settings switches use a small Google Cloud function. One-time setup:

1. In Google Cloud (same project), enable **Cloud Functions API**, **Cloud Run Admin API**, **Cloud Build API** and **Artifact Registry API**.
2. IAM: give the `read-me` service account the roles **Cloud Functions Admin**, **Cloud Run Admin** and **Service Account User**. Give the **Compute Engine default service account** the role **Cloud Build Service Account** (it builds the function).
3. GitHub → Settings → Secrets and variables → Actions → Variables: add `VOICE_FUNCTION` = `on`.
4. For **pull down to check for new mail**: on GitHub, go to your profile picture → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token. Name it "Read Me check", set an expiration (a year is fine), choose **Only select repositories →** your copy of this repo, and under Repository permissions set **Actions: Read and write**. Save the token as a repository secret named `READ_ME_GITHUB_TOKEN`.
5. Actions → **Deploy voice function** → Run workflow. When it's green, Listen, Settings and check-for-mail work in the app. It redeploys itself whenever the code changes.

In the app:

- **Pull down** on the list (or ⋮ → Settings → Check for new mail now) to look for new mail right away instead of waiting for the hourly run. The list reloads by itself about a minute later.
- **Audio is saved on your phone.** Emails from "always voice" senders download when you open the app, and anything you start listening to is saved too, so it plays with no signal. Saved emails say **saved** in the list.
- **⋮ → Settings** also has the **voice** (with a sample of each) for new audio, and your usual **listening speed**.
- Each email reopens **where you left off reading**.

## What Claude does (and doesn't) change

Claude never rewrites the stories. It marks:

- **Remove:** clutter like "view in browser", ads and sponsor sections, footers and unsubscribe text.
- **Quiet:** kept on screen but not read aloud, like bylines, photo credits and small print.
- **Say:** data tables such as a market ticker stay on screen as a table, and the voice reads one plain sentence instead of every cell. As a safeguard, this only applies to table rows and short data lines, never to real paragraphs.
- **Skip:** sign-in codes, "confirm your email", welcome and "you're subscribed" emails are left out entirely.

To change any of this, edit **`prompt.md`**: it's the plain-English instructions Claude follows. For example, you could tell it to keep sponsor sections, or to always drop a newsletter's "jobs board".

## Privacy

- Email text is sent to Anthropic (Claude) and Google (voice) to be processed. Neither uses API data to train its models by default.
- Anyone who has your player or feed link can listen, so don't share it. To change the links, change `FEED_SECRET`. Old episodes stay at the old address until they expire.
- Nothing about your emails is stored in this repo.

## Troubleshooting

| Problem | Fix |
|---|---|
| `This Claude API key isn't tied to a workspace` | Create a new key inside a workspace (console.anthropic.com → Settings → API keys, pick a workspace) and replace the `ANTHROPIC_API_KEY` secret. |
| A labeled email isn't read | Check the label name matches `GMAIL_LABEL` (default `ReadMe`). Or forward the email to your `+readme` address instead. |
| `Couldn't open All Mail` | In Gmail settings → See all settings → Labels, make sure **All Mail** has "Show in IMAP" checked. |
| Gmail login fails | Use an app password, not your normal password; 2-Step Verification must be on. |
| An old email I labeled isn't read | Only emails *received* in the last 7 days are checked. Forward it to your `+readme` address instead, or raise `LOOKBACK_DAYS`. |
| Google error mentioning `texttospeech` / "API has not been used" | Enable **Cloud Text-to-Speech API** in the same project as the service account. |
| Podcast app says the feed can't be loaded | Check step 3.5 (`allUsers` → Storage Legacy Object Reader) and that public access prevention is off. |

## Files

| File | What it does |
|---|---|
| `main.py` | runs everything; `python main.py --dry-run` prints what would be read without making audio |
| `mail.py` | fetches labeled emails from Gmail and pulls out the text |
| `article.py` | turns an email's HTML into clean blocks: headings, paragraphs, lists, photos and links |
| `editor.py` + `prompt.md` | Claude marks which blocks are clutter; nothing is rewritten |
| `voice.py` | Google voices each paragraph and times it, for the read-along highlight |
| `podcast.py` | uploads MP3s and text, builds the podcast feed and player page |
| `player.html` | the Read Me app (ledger theme): listen, read along with photos and links, and ⋮ Settings |
| `function/main.py` | the on-demand voice function the app calls (Listen, Settings) |
| `store.py` | shared access to the storage bucket, settings and the monthly allowance |
| `sw.js`, `icons/` | make the player page installable as an app |
| `.github/workflows/read-emails.yml` | the hourly schedule |

## License

MIT. See [LICENSE](LICENSE).
