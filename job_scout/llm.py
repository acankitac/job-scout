"""Model backends for web research: Amazon Bedrock and the Anthropic API.

Both speak the Anthropic Messages format. The Router tries them in the configured order and
falls back when one is unavailable (missing or expired credentials, model not enabled) or
can't run the web-search tool.
"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class Unavailable(Exception):
    """This backend can't be used for the rest of the run."""


class SearchUnsupported(Exception):
    """This backend rejected the web-search tool."""


class CallFailed(Exception):
    """This one call failed; the backend may still work for others."""


def _looks_like_tool_rejection(msg: str) -> bool:
    m = msg.lower()
    return any(w in m for w in ("web_search", "tool type", "tools.0", "server tool", "not supported"))


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        self.key = api_key
        self.model = model

    def create(self, body: dict) -> dict:
        body = dict(body, model=self.model)
        req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (401, 403):
                raise Unavailable(f"HTTP {e.code}: {detail}") from None
            if e.code == 400 and "tools" in body and _looks_like_tool_rejection(detail):
                raise SearchUnsupported(detail) from None
            raise CallFailed(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise CallFailed(str(e)) from None


class BedrockBackend:
    name = "bedrock"
    _UNAVAILABLE = ("ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException",
                    "InvalidClientTokenId", "AccessDeniedException", "InvalidSignatureException",
                    "ResourceNotFoundException")

    def __init__(self, profile: Optional[str], region: str, model_id: str, family: str = "sonnet"):
        import boto3  # only needed when Bedrock is configured
        import botocore.exceptions as bexc
        self._bexc = bexc
        self.session = boto3.Session(profile_name=profile or None, region_name=region)
        self.region = region
        self.family = family
        self.model_id = model_id
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self.session.client("bedrock-runtime")
        return self._client

    def _resolve_model(self) -> str:
        """'auto': newest Claude inference profile of the configured family in this region."""
        if self.model_id != "auto":
            return self.model_id
        ctl = self.session.client("bedrock")
        ids = []
        for page in ctl.get_paginator("list_inference_profiles").paginate(typeEquals="SYSTEM_DEFINED"):
            ids += [p["inferenceProfileId"] for p in page.get("inferenceProfileSummaries", [])
                    if "anthropic.claude" in p["inferenceProfileId"] and self.family in p["inferenceProfileId"]]
        if not ids:
            raise Unavailable(f"no Claude {self.family} inference profile found in {self.region}; "
                              f"set research.bedrock_model_id")
        self.model_id = sorted(ids)[-1]
        return self.model_id

    def create(self, body: dict) -> dict:
        bexc = self._bexc
        payload = dict(body, anthropic_version="bedrock-2023-05-31")
        payload.pop("model", None)
        for attempt in range(3):
            try:
                resp = self.client.invoke_model(modelId=self._resolve_model(), body=json.dumps(payload))
                return json.loads(resp["body"].read())
            except (bexc.NoCredentialsError, bexc.PartialCredentialsError, bexc.ProfileNotFound,
                    bexc.TokenRetrievalError, bexc.SSOError) as e:
                raise Unavailable(f"credentials: {e}") from None
            except bexc.ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                msg = e.response.get("Error", {}).get("Message", "")
                if code in self._UNAVAILABLE:
                    raise Unavailable(f"{code}: {msg[:200]}") from None
                if code == "ValidationException" and "tools" in payload and _looks_like_tool_rejection(msg):
                    raise SearchUnsupported(msg[:200]) from None
                if code in ("ThrottlingException", "ServiceUnavailableException") and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise CallFailed(f"{code}: {msg[:200]}") from None
            except bexc.BotoCoreError as e:
                raise CallFailed(str(e)) from None
        raise CallFailed("throttled")


class Router:
    """Tries backends in order. Prefers any backend that can web-search; uses a search-less
    answer only when allowed and nothing else worked."""

    def __init__(self, backends: list, allow_without_search: bool = False):
        self.backends = backends
        self.allow_without_search = allow_without_search
        self.down = {}         # name -> reason, for the rest of the run
        self.no_search = set()  # names that rejected the web-search tool
        self.notices = []
        self.used = set()

    @property
    def available(self) -> bool:
        return any(b.name not in self.down for b in self.backends)

    def _note(self, msg: str):
        if msg not in self.notices:
            self.notices.append(msg)

    def resume(self, name: str, body: dict, searched: bool) -> dict:
        """Continue a paused conversation on the backend that started it."""
        b = next(b for b in self.backends if b.name == name)
        if not searched:
            body = {k: v for k, v in body.items() if k != "tools"}
        try:
            return b.create(body)
        except (Unavailable, SearchUnsupported) as e:
            raise CallFailed(f"{name}: {e}") from None

    def create(self, body: dict) -> tuple[dict, str, bool]:
        """Returns (response, backend name, searched)."""
        errors = []
        for b in self.backends:
            if b.name in self.down or b.name in self.no_search:
                continue
            try:
                resp = b.create(body)
                self.used.add(b.name)
                return resp, b.name, True
            except Unavailable as e:
                self.down[b.name] = str(e)
                self._note(f"{b.name} unavailable, falling back: {e}")
            except SearchUnsupported as e:
                self.no_search.add(b.name)
                self._note(f"{b.name} can't run web search, falling back: {e}")
            except CallFailed as e:
                errors.append(f"{b.name}: {e}")
        if self.allow_without_search:
            bare = {k: v for k, v in body.items() if k != "tools"}
            bare["messages"] = [dict(m) for m in body["messages"]]
            bare["messages"][0]["content"] += NO_SEARCH_NOTE
            for b in self.backends:
                if b.name in self.no_search and b.name not in self.down:
                    try:
                        resp = b.create(bare)
                        self.used.add(b.name + " (no web search)")
                        return resp, b.name, False
                    except Unavailable as e:
                        self.down[b.name] = str(e)
                    except CallFailed as e:
                        errors.append(f"{b.name}: {e}")
        reasons = errors + [f"{n}: {r}" for n, r in self.down.items()] + \
            [f"{n}: no web search" for n in self.no_search]
        raise CallFailed("; ".join(reasons) or "no research backend configured")


NO_SEARCH_NOTE = ("\n\nWeb search is not available. Answer from what you know, set confidence to "
                  "\"low\", say in notes that this is not based on a live search, and leave sources empty.")


def build_router(cfg: dict, anthropic_key: Optional[str] = None) -> Router:
    backends, notices = [], []
    for name in cfg.get("providers", ["bedrock", "anthropic"]):
        if name == "bedrock":
            try:
                backends.append(BedrockBackend(
                    cfg.get("bedrock_profile", "bedrock"),
                    cfg.get("bedrock_region") or os.environ.get("AWS_REGION") or "us-east-1",
                    cfg.get("bedrock_model_id", "auto"), cfg.get("bedrock_model_family", "sonnet")))
            except ImportError:
                notices.append("bedrock skipped: boto3 is not installed (pip install boto3)")
            except Exception as e:  # e.g. ProfileNotFound raised while building the session
                notices.append(f"bedrock skipped: {e}")
        elif name == "anthropic":
            key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY")
            if key:
                backends.append(AnthropicBackend(key, cfg.get("model", "claude-sonnet-5")))
        else:
            notices.append(f"unknown research provider {name!r} ignored")
    router = Router(backends, cfg.get("allow_without_web_search", False))
    router.notices += notices
    return router
