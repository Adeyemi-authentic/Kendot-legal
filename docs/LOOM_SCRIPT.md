# 60-second Loom script

About 150 spoken words. Four shots, each 12 to 18 seconds. Record at 1440×900, browser zoom 100%, no other tabs.

## Before you press record

1. **Wake the API.** Open https://kendot-assistant.onrender.com/health and wait for `{"status":"ok"}`.
   The free tier sleeps; a cold start on camera looks like a broken product.
2. **Mind the Voyage free tier** (about 3 requests a minute; each question uses 2). Wait about 20 seconds
   between questions, and rehearse a few minutes *before* recording, not just before. If you see "The assistant is
   busy", stop, wait a minute, retake.
3. Open two tabs: the Real Estate and Tenancy page (https://kendot-legal.vercel.app/practice/real-estate-tenancy/)
   and https://kendot-legal.vercel.app/internal/ already signed in.
4. Clear the widget with its "New conversation" button so it opens on the welcome message.

## The script

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:12 | Real Estate page, scroll once, open the widget | "This is a law firm website with an assistant built in. It answers from the firm's own pages, and nothing else." |
| 0:12 to 0:28 | Type **How much advance rent can a landlord in Lagos ask for?** Let it stream. Click the source badge. | "Every answer cites the page it came from. Click the source, and you land on the article. If it isn't on the site, it says it doesn't know." |
| 0:28 to 0:42 | New conversation. Type **My landlord locked me out of my flat yesterday. Should I sue him?** The "Talk to a lawyer" button appears; click it to show the form. | "It never gives legal advice. When someone needs a lawyer, it hands them to the firm with a short enquiry form, with consent under the NDPA." |
| 0:42 to 1:00 | Switch to the `/internal` tab. Type **What does Bayo have to do this month?** | "And this is the private side, for the firm's lawyers only: precedents, templates and the matter list, with deadlines and who's responsible. It's completely separate. The public assistant can't see any of this, and there's a test that proves it." |

Close on the internal answer for one second, then stop recording.

## If there's time for a second take (90-second version)

Add between shots 3 and 4: ask the public widget **What are your hourly rates?** It declines, because rates are
internal. Then ask the same question on `/internal`, where it answers from the engagement letter template. That
contrast is the clearest proof of separation.

## Title and description for the Loom

- **Title:** Law firm website with a cited AI assistant (60-second demo)
- **Description:** A concept build for a fictional Lagos/Abuja firm. The public assistant answers only from the
  firm's site, cites every answer and hands advice requests to a lawyer. The internal assistant answers from the
  firm's own documents and is kept fully separate. Live: https://kendot-legal.vercel.app
