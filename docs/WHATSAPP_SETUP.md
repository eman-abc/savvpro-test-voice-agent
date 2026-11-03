# WhatsApp setup (Meta Cloud API, free test number)

This connects the assistant to WhatsApp so you can chat with it, send **voice notes**, and receive reminders and waitlist offers.
It uses Meta's free **test phone number**, which can message up to **5 numbers you register**. Your own WhatsApp is the *recipient*:
**do not try to use your personal number as the sender** (a number already on WhatsApp can't be registered for the API).

> Meta's dashboard labels change from time to time. If a menu name differs, look for the same idea in Meta's docs.
> I could not test this against live WhatsApp, so expect to debug the first run (see Troubleshooting).

## 1. What you need
- A Facebook account (Meta for Developers registration is done while logged in with one; an Instagram account alone is not enough).
- The public HTTPS address of your deployed assistant, for example `https://clinic-voice-agent.onrender.com`.
- Optional, for voice notes: a Deepgram account (free $200 credit, no card).

## 2. Create the Meta app
1. Go to **developers.facebook.com**, register as a developer, and create a **Business Portfolio** if asked.
2. **Create app**, choose the **Business** type, then add the **WhatsApp** product. Meta creates a test WhatsApp Business Account and a free test number.
3. Write down:
   - **Phone number ID** (WhatsApp, then API Setup),
   - **App secret** (App settings, then Basic),
   - an **access token**: the temporary one on the API Setup page works for a quick test but **expires in 24 hours**. For a demo that lasts, create a **System User** in Business Settings, give it the app/WhatsApp assets, and generate a token with the WhatsApp messaging permissions.
4. Under **API Setup, To**, choose **Manage phone number list**, add **your own number** (country code, no spaces), and enter the code WhatsApp sends you.

## 3. Configure the assistant (Render: Environment, then redeploy)
| Variable | Value |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | any random string you choose |
| `WHATSAPP_APP_SECRET` | the app secret |
| `WHATSAPP_ACCESS_TOKEN` | the token |
| `WHATSAPP_PHONE_NUMBER_ID` | the phone number ID |
| `WHATSAPP_ALLOWED_NUMBERS` | your number as digits, e.g. `923001234567` (anyone else is ignored: keeps the demo to fake data and you) |
| `DEMO_WHATSAPP_NUMBER` | the same digits: links the demo patient to **your** phone so reminders reach you |
| `DEMO_WHATSAPP_PATIENT` | optional, default `Wei Zhang` |
| `STT_PROVIDER` / `DEEPGRAM_API_KEY` | `deepgram` and your key, to understand voice notes (leave unset to use text only) |

All four `WHATSAPP_*` credentials are required; until they are all set the webhook does not exist (404).

## 4. Connect the webhook
1. In Meta: **WhatsApp, Configuration, Webhook**. Callback URL: `https://<your-service>.onrender.com/api/whatsapp/webhook`. Verify token: the same string as above.
2. **Verify and save.** Meta calls the URL once; the assistant answers only if the token matches.
3. **Subscribe** to the **messages** field.
4. Wake a sleeping free-tier service first (open the site); the first request after sleep takes about a minute.

## 5. Try it
1. From your phone, message the test number: `I need a flu shot`, then verify as the demo patient (see the "Callers to try" list).
2. Try Urdu: `مجھے اپائنٹمنٹ چاہیے`.
3. Send a **voice note** (needs `STT_PROVIDER`). The reply starts with what the assistant heard. If it is unsure, it reads the words back and asks you to confirm.
4. Reminders and offers: in the staff area open **Messages**, check **WhatsApp: connected**, then **Send due reminders now**. Your phone must have **messaged the number within 24 hours** (WhatsApp's rule); reply `1` to confirm. Cancel someone else's slot while you're on the waitlist and the offer arrives; reply `1` to book.

## 6. How the 24-hour rule is handled
Inside 24 hours of your last message the assistant sends normal text. Outside it, WhatsApp only allows an **approved template**.
Set `WHATSAPP_TEMPLATE_NAME` to a template you have created and had approved (English and Urdu variants, one text variable); otherwise the
message is recorded as `window_closed` in the Messages tab and not sent. Template approval can take minutes to a day.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Meta says verification failed | verify token differs, or the service is asleep / the URL is wrong |
| No reply to your messages | webhook not subscribed to `messages`; your number isn't in `WHATSAPP_ALLOWED_NUMBERS`; service asleep; token expired |
| Webhook returns 401 | wrong `WHATSAPP_APP_SECRET` (signatures are checked on every call) |
| Reminder shows `window_closed` | you haven't messaged the number in the last 24 hours, and no template is configured |
| Reminder shows `failed` with 131030 | the recipient isn't on the test number's allowed list in Meta |
| Reminder shows `failed` with an auth error | the access token expired (the temporary one lasts 24 hours) |
| Voice note: "couldn't understand" | `STT_PROVIDER` not set, bad Deepgram key, or audio too large |
