"""A fake Meta Graph API and Deepgram that record every request, so no test touches the network."""

import hashlib
import hmac
import json

import httpx

APP_SECRET = "shh-app-secret"
VERIFY = "verify-me"
TOKEN = "wa-token"
ME = "923001112222"  # the tester's WhatsApp number (digits)


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeMeta:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deepgram: list[dict] = []
        self.media: dict[str, bytes] = {}
        self.dg_queue: list[dict] = [{"transcript": "what time do you open", "confidence": 0.93}]
        self.dg_status = 200
        self.send_error: tuple[int, str] | None = None
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/PNID/messages"):
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            body = json.loads(request.content)
            if self.send_error:
                code, msg = self.send_error
                return httpx.Response(400, json={"error": {"message": msg, "code": code}})
            self.sent.append(body)
            self.n += 1
            return httpx.Response(200, json={"messages": [{"id": f"wamid.OUT{self.n}"}]})
        if request.method == "GET" and "media.example/dl/" in url:
            assert request.headers["authorization"] == f"Bearer {TOKEN}"  # media URLs need the token too
            return httpx.Response(200, content=self.media[url.rsplit("/", 1)[1]], headers={"content-type": "audio/ogg"})
        if request.method == "GET" and "/v21.0/" in url:
            mid = url.rsplit("/", 1)[1]
            if mid in self.media:
                return httpx.Response(
                    200,
                    json={
                        "url": f"https://media.example/dl/{mid}",
                        "mime_type": "audio/ogg; codecs=opus",
                        "file_size": len(self.media[mid]),
                    },
                )
            return httpx.Response(404, json={"error": {"message": "unknown media", "code": 100}})
        if request.method == "POST" and "api.deepgram.com/v1/listen" in url:
            self.deepgram.append(
                {"params": dict(request.url.params), "headers": dict(request.headers), "size": len(request.content)}
            )
            if self.dg_status != 200:
                return httpx.Response(self.dg_status, json={"err": "x"})
            alt = self.dg_queue.pop(0) if len(self.dg_queue) > 1 else self.dg_queue[0]
            return httpx.Response(200, json={"results": {"channels": [{"alternatives": [alt]}]}})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def texts(self) -> list[str]:
        return [b["text"]["body"] for b in self.sent if b["type"] == "text"]


def envelope(value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"}, **value},
                    }
                ],
            }
        ],
    }


def text_payload(sender: str, text: str, mid: str) -> dict:
    return envelope(
        {"messages": [{"from": sender, "id": mid, "timestamp": "1700000000", "type": "text", "text": {"body": text}}]}
    )


def audio_payload(sender: str, media_id: str, mid: str) -> dict:
    return envelope(
        {
            "messages": [
                {
                    "from": sender,
                    "id": mid,
                    "timestamp": "1700000000",
                    "type": "audio",
                    "audio": {"mime_type": "audio/ogg; codecs=opus", "id": media_id, "voice": True},
                }
            ]
        }
    )


def image_payload(sender: str, mid: str) -> dict:
    return envelope(
        {"messages": [{"from": sender, "id": mid, "timestamp": "1700000000", "type": "image", "image": {"id": "IMG"}}]}
    )


def status_payload(wamid: str, status: str, recipient: str = ME, errors: list | None = None) -> dict:
    st = {"id": wamid, "status": status, "timestamp": "1700000001", "recipient_id": recipient}
    if errors:
        st["errors"] = errors
    return envelope({"statuses": [st]})


def post_signed(client, payload: dict, secret: str = APP_SECRET, tamper: bool = False):
    body = json.dumps(payload).encode()
    sig = sign(body, secret)
    if tamper:
        body = body.replace(b"hello", b"jello")
    return client.post(
        "/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"}
    )
