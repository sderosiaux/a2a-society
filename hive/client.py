from __future__ import annotations

import uuid

import httpx


class A2AClientError(Exception):
    """Generic A2A client error."""


class A2ATaskRejected(A2AClientError):
    """Task was rejected by the peer (at_capacity, on_vacation, etc.)."""

    def __init__(self, reason: str, resume_at: str | None = None):
        self.reason = reason
        self.resume_at = resume_at
        super().__init__(f"Task rejected: {reason}")


class A2AClient:
    """Client for sending A2A tasks to peer agents."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)

    async def send_task(
        self,
        peer_url: str,
        message_text: str,
        from_agent: str,
        priority: str = "normal",
        callback_url: str | None = None,
        artifact_ref: dict | None = None,
    ) -> dict:
        """
        Send an A2A task to a peer agent.

        Posts a JSON-RPC message/send request to the peer's A2A endpoint.

        Returns: dict with task_id, status, and full response
        Raises: A2AClientError on rejection or connection failure
        """
        metadata: dict = {
            "from_agent": from_agent,
            "priority": priority,
            "callback_url": callback_url,
        }
        if artifact_ref is not None:
            metadata["artifact_ref"] = artifact_ref

        payload = {
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": str(uuid.uuid4()),
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": message_text}],
                    "metadata": metadata,
                }
            },
        }

        try:
            resp = await self._http.post(peer_url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise A2AClientError(
                f"HTTP {exc.response.status_code} from {peer_url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise A2AClientError(f"Connection error to {peer_url}: {exc}") from exc

        data = resp.json()

        # JSON-RPC error
        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            raise A2AClientError(f"JSON-RPC error: {msg}")

        result = data.get("result", {})
        status = result.get("status", {})
        state = status.get("state", "unknown")

        # Rejected task
        if state == "rejected":
            reason = status.get("message", {}).get("parts", [{}])[0].get(
                "text", "unknown reason"
            )
            resume_at = status.get("timestamp")
            raise A2ATaskRejected(reason=reason, resume_at=resume_at)

        return {
            "task_id": result.get("id"),
            "status": state,
            "response": data,
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()
