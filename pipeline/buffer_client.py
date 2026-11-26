"""
Client GraphQL officiel Buffer (https://api.buffer.com) pour l'automatisation Pinterest Free.
- Aucune API privée, aucun cookie exporté, aucun contournement.
- Jeton lu exclusivement via BUFFER_API_TOKEN (expurgé de toute sortie/erreur).
- Introspection du schéma PinterestPostMetadataInput avant toute écriture pour garantir
  que boardServiceId et url/link sont bien acceptés par l'API active.
"""
import json
import urllib.error
import urllib.request

BUFFER_ENDPOINT = "https://api.buffer.com"
USER_AGENT = "ecomzero-pinterest-autopilot/1.0"


class BufferApiError(RuntimeError):
    def __init__(self, code, message, is_quota=False):
        super().__init__(message)
        self.code = code
        self.is_quota = is_quota


VALID_SCHEDULED_STATUSES = {"scheduled", "buffer", "queued"}


def scrub_secret(text, token):
    return str(text).replace(token, "***") if token else str(text)


class BufferGraphQLClient:
    def __init__(self, token, endpoint=BUFFER_ENDPOINT, transport=None):
        self.token = token or ""
        self.endpoint = endpoint
        self._transport = transport  # injectable pour tests locaux sans réseau

    def query(self, gql_query, variables=None):
        if not self.token:
            raise BufferApiError(401, "BUFFER_API_TOKEN manquant (valider l'e-mail Buffer et créer une clé API).")
        payload = {"query": gql_query}
        if variables:
            payload["variables"] = variables

        if self._transport is not None:
            status, body = self._transport(self.endpoint, payload, self.token)
        else:
            req = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.status
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = scrub_secret(e.read().decode("utf-8", "replace")[:300], self.token)
                is_quota = e.code == 429 or "limit" in raw.lower() or "quota" in raw.lower()
                raise BufferApiError(e.code, f"HTTP {e.code}: {raw}", is_quota=is_quota) from e
            except Exception as e:
                raise BufferApiError(0, scrub_secret(f"{type(e).__name__}: {e}", self.token)) from e

        if status == 429:
            raise BufferApiError(429, "Quota API Buffer atteint (HTTP 429).", is_quota=True)
        if status != 200:
            raise BufferApiError(status, scrub_secret(f"HTTP {status}: {body}", self.token))
        if isinstance(body, dict) and body.get("errors"):
            msg = scrub_secret(json.dumps(body["errors"])[:300], self.token)
            is_quota = "limit" in msg.lower() or "quota" in msg.lower() or "10 posts" in msg.lower()
            raise BufferApiError(400, f"GraphQL error: {msg}", is_quota=is_quota)
        return body.get("data", {})

    def list_pinterest_channels(self):
        orgs_data = self.query("query { account { organizations { id name } } }")
        channels = []
        for org in orgs_data.get("account", {}).get("organizations", []):
            ch_data = self.query(
                'query($orgId: ID!) { channels(input: { organizationId: $orgId }) { id name service isQueuePaused } }',
                {"orgId": org["id"]},
            )
            for ch in ch_data.get("channels", []):
                if str(ch.get("service", "")).lower() == "pinterest":
                    channels.append(ch)
        return channels

    def list_pinterest_boards(self, channel_id):
        q = (
            "query($id: ChannelId!) { channel(input: { id: $id }) { id name metadata { "
            "... on PinterestMetadata { boards { id serviceId name } } } } }"
        )
        data = self.query(q, {"id": channel_id})
        return data.get("channel", {}).get("metadata", {}).get("boards", [])

    def list_scheduled_posts(self, channel_id):
        orgs_data = self.query("query { account { organizations { id } } }")
        orgs = orgs_data.get("account", {}).get("organizations", [])
        if not orgs:
            raise BufferApiError(404, "Aucune organisation Buffer trouvée pour ce jeton.")
        org_id = orgs[0]["id"]
        q = (
            "query { posts(first: 20, input: { organizationId: %s, filter: { channelIds: [%s], status: [scheduled] } }) { "
            "edges { node { id text dueAt status } } } }"
        ) % (json.dumps(org_id), json.dumps(channel_id))
        data = self.query(q)
        edges = data.get("posts", {}).get("edges", [])
        return [e["node"] for e in edges if "node" in e]

    def introspect_pinterest_metadata_fields(self):
        q = '{ __type(name: "PinterestPostMetadataInput") { inputFields { name } } }'
        data = self.query(q)
        fields = (data.get("__type") or {}).get("inputFields") or []
        return [f["name"] for f in fields]

    def schedule_pin(self, channel_id, board_service_id, image_url, title, description, destination_url, due_at_iso, dry_run=True):
        """
        Programme une épingle Pinterest en mode customScheduled + automatic.
        En dry_run=True (défaut), retourne le payload sans appeler l'API.
        """
        pinterest_meta = {
            "boardServiceId": board_service_id,
            "title": title,
            "url": destination_url,
        }
        variables = {
            "input": {
                "text": description,
                "channelId": channel_id,
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due_at_iso,
                "assets": [{"image": {"url": image_url}}],
                "metadata": {"pinterest": pinterest_meta},
            }
        }
        mutation = (
            "mutation CreatePinterestPin($input: CreatePostInput!) { "
            "createPost(input: $input) { "
            "... on PostActionSuccess { post { id status dueAt text } } "
            "... on MutationError { message } } }"
        )
        if dry_run:
            return {"dry_run": True, "mutation": mutation, "variables": variables}

        # Vérification schéma avant écriture réelle
        meta_fields = self.introspect_pinterest_metadata_fields()
        if "boardServiceId" not in meta_fields:
            raise BufferApiError(422, f"Schéma Buffer inattendu (boardServiceId absent): {meta_fields}")
        if "url" not in meta_fields:
            raise BufferApiError(422, f"Champ Destination Link 'url' absent du schéma Buffer actif: {meta_fields}")

        data = self.query(mutation, variables)
        res = data.get("createPost")
        if not isinstance(res, dict):
            raise BufferApiError(502, f"Réponse createPost vide ou inattendue: {res}")
        if "message" in res and "post" not in res:
            msg = str(res["message"])
            is_quota = "limit" in msg.lower() or "quota" in msg.lower() or "free plan" in msg.lower()
            raise BufferApiError(422, f"MutationError: {msg}", is_quota=is_quota)
        post = res.get("post")
        if not isinstance(post, dict):
            raise BufferApiError(502, f"Objet post absent dans la réponse Buffer: {res}")
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            raise BufferApiError(502, f"Identifiant post vide dans la réponse Buffer: {post}")
        status = str(post.get("status") or "").strip().lower()
        if status not in VALID_SCHEDULED_STATUSES:
            raise BufferApiError(502, f"Statut de programmation Buffer incohérent: '{status}' (post {post_id})")
        return {"dry_run": False, "post": post}
