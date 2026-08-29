
import os, time
import httpx
from base import TechnicalDataProvider

class AutodataProvider(TechnicalDataProvider):
    """
    Adapter for the official Autodata API.

    Autodata documents OAuth 2.0/API-key authentication and REST/JSON support.
    Exact production endpoints/scopes are partner-account configuration,
    therefore all URLs and credential names are environment-driven rather
    than hard-coded.
    """
    name = "autodata"

    def __init__(self):
        self.base_url = os.getenv("AUTODATA_BASE_URL", "").rstrip("/")
        self.token_url = os.getenv("AUTODATA_TOKEN_URL", "")
        self.client_id = os.getenv("AUTODATA_CLIENT_ID", "")
        self.client_secret = os.getenv("AUTODATA_CLIENT_SECRET", "")
        self.api_key = os.getenv("AUTODATA_API_KEY", "")
        self.vehicle_path = os.getenv("AUTODATA_VEHICLE_PATH", "/vehicles")
        self.technical_path = os.getenv("AUTODATA_TECHNICAL_PATH", "/technical")
        self.procedure_path = os.getenv("AUTODATA_PROCEDURE_PATH", "/procedures")
        self.scope = os.getenv("AUTODATA_SCOPE", "")
        self._token = None
        self._token_expires = 0

    def configured(self) -> bool:
        return bool(self.base_url and self.token_url and self.client_id and self.client_secret)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 30:
            return self._token

        if not self.configured():
            raise RuntimeError("Autodata no está configurado")

        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope

        auth = (self.client_id, self.client_secret)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.token_url, data=data, auth=auth, headers=headers)
            r.raise_for_status()
            payload = r.json()

        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    async def _get(self, path: str, params: dict | None = None):
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.get(f"{self.base_url}{path}", params=params or {}, headers=headers)
            r.raise_for_status()
            return r.json()

    async def search_vehicle(self, **kwargs):
        params = {k:v for k,v in kwargs.items() if v not in (None, "")}
        return await self._get(self.vehicle_path, params=params)

    async def get_technical_data(self, vehicle_ref: str, section: str | None = None):
        params = {"vehicle": vehicle_ref}
        if section:
            params["section"] = section
        return await self._get(self.technical_path, params=params)

    async def get_repair_procedure(self, vehicle_ref: str, procedure_ref: str):
        return await self._get(
            self.procedure_path,
            params={"vehicle": vehicle_ref, "procedure": procedure_ref},
        )
