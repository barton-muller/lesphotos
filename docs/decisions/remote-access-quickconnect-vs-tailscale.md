# Decision: remote access — QuickConnect is not viable, Tailscale stays

## Question

Can the app sync via Synology QuickConnect instead of Tailscale, so a client device doesn't
need Tailscale installed/configured to reach the NAS from outside the LAN?

## Research findings (see the archived plan.md for the full agent report this summarizes)

- **QuickConnect is not a general-purpose NAT-traversal/relay tool.** It's Synology's own
  connectivity broker, scoped specifically to Synology's first-party app registry (DSM, File
  Station, Synology Photos, Drive, etc.) — it has no mechanism to route to an arbitrary Docker
  container's port. Multiple independent sources converge on this: PhotoView (running on a
  non-standard Docker port) and our own FastAPI service are both **not reachable through
  QuickConnect**, full stop, not even in principle.
- The Application Portal / reverse proxy feature (Control Panel → Application Portal) *can*
  front a Docker container with real HTTPS on the NAS side, but reaching that reverse-proxied
  endpoint remotely without Tailscale still requires manual port-forwarding + your own DDNS or
  domain + your own cert — which is a different, DIY setup, not "use QuickConnect instead of
  Tailscale."
- Even where QuickConnect *does* apply (Synology's own apps), its relay fallback path
  (used whenever direct/hole-punched connection fails, e.g. behind CGNAT) is not end-to-end
  encrypted the way Tailscale's WireGuard tunnels are — Synology's relay servers terminate and
  re-encrypt TLS, meaning Synology's own infrastructure is technically capable of seeing
  plaintext at that hop. Tailscale's fallback relay (DERP) cannot decrypt traffic by design.

## Decision: keep Tailscale as the remote-access mechanism. Do not pursue QuickConnect.

This isn't a close call or a tradeoff to weigh — QuickConnect is architecturally incapable of
reaching our custom services at all, independent of any security/performance comparison. The
security comparison (Tailscale's relay path is genuinely more private) is a secondary point
that would have mattered if QuickConnect had been viable; it isn't the deciding factor here.

**No action needed** — this was already the existing design (the original design doc
specifies Tailscale, NAS already has it installed and configured as an exit node). This
decision doc exists to record that QuickConnect was seriously investigated and ruled out with
reasons, not silently assumed unusable, in case the question comes up again later.

## If remote access without installing Tailscale on the client device is a real future need

Worth keeping in mind as a distinct future question, separate from tonight's QuickConnect
research: Tailscale does support **Funnel** (exposing a tailnet service to the public internet
via Tailscale's own infrastructure, no Tailscale client needed on the visiting device) — this
is the closest thing to "share access without requiring the other person to install
anything," if that ever becomes a real requirement (e.g. sharing a single album link with
someone outside the family's Tailscale network). Not evaluated in depth here since it wasn't
part of tonight's specific ask — flagged for a future pass if needed.
