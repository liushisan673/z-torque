# A2A v1.0 inbound interoperability boundary

The community edition exposes the standard A2A Agent Card at
`GET /.well-known/agent-card.json` and a JSON-RPC 2.0 endpoint at
`POST /api/z-torque/a2a/jsonrpc`. The card requires a trusted Bearer credential and only lists published Agents
within the caller's configured workspace; configured federated credentials are
accepted for read-only discovery, while Task operations require a local user identity.

Implemented JSON-RPC methods:
- `SendMessage`: creates an ordinary Z-Torque Task and returns an A2A Task.
- `GetTask`: returns a Task owned by the authenticated local user.
- `ListTasks`: returns only the authenticated user's Tasks, with offset page tokens.
- `CancelTask`: cancels queued Tasks or requests cancellation of running Tasks.

The A2A Message `metadata` extension `z-torque/agentId` selects the published
Agent for `SendMessage`. This is an explicit Z-Torque extension because the
community edition can expose multiple Agents behind one endpoint. Message text
parts are currently supported; binary/file parts are not.

Security boundary:
- Every Task is owned by the authenticated local user and uses the normal
  published-Agent visibility check and Task idempotency key.
- Federated peers with `user_id=None` cannot create or inspect Tasks. The
  implementation deliberately refuses to invent a local owner; federated Task
  access requires a separately configured local service-account mapping.
- Caller-provided `X-User-Id` is never used as identity.
- Task reads, listing and cancellation are scoped by both tenant and owner.

Not advertised as supported: streaming methods, push notifications, remote
service-account task ownership, and push-notification configuration methods.
The Agent Card therefore reports streaming and push notifications as disabled.
The historical `/a2a/invoke` route remains a compatibility endpoint and is not
the standard JSON-RPC binding.
