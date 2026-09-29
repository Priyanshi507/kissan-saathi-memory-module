from typing import Dict, Tuple
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from procurement import ivr

from .deps import Deps

XML = "application/xml"


def build(deps: Deps) -> APIRouter:
    router = APIRouter(prefix="/ivr")

    async def verified_form(request: Request) -> Dict[str, str]:
        raw = (await request.body()).decode("utf-8", "replace")
        params = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        if deps.ivr_allow_unsigned:
            return params
        if not (deps.ivr_auth_token and deps.public_base_url):
            raise HTTPException(503, "IVR is not configured: set TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL")
        url = deps.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not ivr.valid_signature(deps.ivr_auth_token, url, params, request.headers.get("X-Twilio-Signature")):
            raise HTTPException(403, "Invalid Twilio signature")
        return params

    def caller_language(farmer_id) -> Tuple[str, str]:
        stored = deps.store.get_language(farmer_id) if farmer_id else None
        code = ivr.spoken_lang(stored, deps.ivr_language)
        return code, code.split("-")[0]

    def action_url() -> str:
        base = (deps.public_base_url or "").rstrip("/")
        return f"{base}/ivr/gather"

    @router.post("/voice")
    async def voice(request: Request):
        params = await verified_form(request)
        farmer_id = ivr.farmer_id_from_caller(params.get("From"))
        code, lang = caller_language(farmer_id)
        body = ivr.gather_response(ivr.prompt_for("greeting", lang), action_url(), code, ivr.prompt_for("bye", lang))
        return Response(body, media_type=XML)

    @router.post("/gather")
    async def gather(request: Request):
        params = await verified_form(request)
        farmer_id = ivr.farmer_id_from_caller(params.get("From"))
        code, lang = caller_language(farmer_id)
        heard = (params.get("SpeechResult") or "").strip()

        if not farmer_id:
            return Response(ivr.say_and_hangup(ivr.prompt_for("error", lang), code), media_type=XML)
        if not heard:
            body = ivr.gather_response(ivr.prompt_for("silence", lang), action_url(), code, ivr.prompt_for("bye", lang))
            return Response(body, media_type=XML)

        answer = ivr.answer_from_records(deps.store, farmer_id, heard, lang)
        if answer is None:
            try:
                answer = deps.answer(farmer_id, heard, lang) if deps.answer else None
            except Exception:
                answer = None
        answer = answer or ivr.prompt_for("error", lang)
        body = ivr.say_and_gather(answer, ivr.prompt_for("again", lang), action_url(), code, ivr.prompt_for("bye", lang))
        return Response(body, media_type=XML)

    return router
