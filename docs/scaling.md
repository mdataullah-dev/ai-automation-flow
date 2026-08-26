# Scaling the audio app to 5,000 workers in one weekend

Honestly, the app is a solid demo — but it's built to run on one machine, and that's exactly what falls
apart when 5,000 people show up over a weekend. Here's how I think it actually plays out.

## What breaks first

The real fuse is the audio processing. Right now, the moment someone submits, the server runs ffmpeg to
analyse their clip — and it does that *while the worker waits*. ffmpeg is heavy on CPU and memory, so if
even 50 people upload at the same time, the server tries to run 50 of those jobs at once, runs out of
memory, and crashes. And that crash takes the whole app down for everyone, not just those 50.

Then it gets worse. On a free host the server's disk is *ephemeral* — meaning it gets wiped whenever the
app restarts. So when the app reboots after that crash, every recording collected so far is just… gone.
And on top of that, the database is a single SQLite file that locks completely on every write, so even
before anything crashes, workers all hitting *Submit* at once are getting "database is locked" errors and
quietly losing their submissions.

So it's really one connected failure: too much heavy work on one box → it crashes → it restarts →
everything gets wiped.

## What I'd change before launch

**Storage.** Stop keeping anything on the app server. Audio goes straight to object storage (like Amazon
S3 or Cloudflare R2), and the database just stores a link to it. And I'd swap SQLite for a managed
Postgres — it's built for lots of people writing at once, and it doesn't disappear when the app restarts.

**Uploads.** Gig workers are on phones with weak signal, so I'd cap the file size and length, only accept
real audio formats, and use uploads that can *resume* if the connection drops — so a bad network doesn't
mean starting over.

**Failures.** The big one: stop making people wait for the analysis. Take the recording, immediately say
"got it," and drop the job onto a queue that separate background workers pick up and process at their own
pace, retrying anything that fails. Now a heavy spike just makes the queue a little longer instead of
crashing the site.

**Duplicates.** When an upload feels slow, people mash the Submit button. So I'd disable the button the
moment it's clicked and show a spinner, and give each submission a unique key so the backend simply
ignores the repeats. (Also worth saying: a phone number *links* a clip to a person, but it doesn't
*prove* who they are — if that matters, add a one-time code.)

**Cost.** A single server big enough for the whole weekend rush, sitting idle the rest of the time, is
just wasted money. I'd run it so it automatically scales *up* extra workers during the Saturday spike and
back *down* to almost nothing by Monday — so we pay for the traffic we actually get. Object storage is
cheap, and moving the audio work onto a queue keeps the compute bill predictable.

