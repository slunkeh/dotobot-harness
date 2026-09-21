# Google Workspace connections

Gmail, Calendar, Drive, Sheets and Docs connect through Dotobot's private account
service. Add each account separately in the app, choose the Google account in
its consent page, and approve the requested access. The account's verified email
labels the connection. Bot permissions remain local to each connector.

The public runtime implements only a generic delegated-credential protocol:

- An authenticated app installs an opaque capability with
  `POST /api/connectors/{id}/delegated-auth` (`broker_url`, `grant_id`, `capability`).
- The harness calls the HTTPS broker's `/token` endpoint with that capability
  as a bearer and the grant ID, connector ID and service in a JSON body.
- The broker verifies the account, registered server and service binding and
  returns a short-lived provider access token. Only that token is sent to Google.
- Disconnect/removal revokes the capability through `/revoke` before deleting it.
  A broker outage leaves the connection available to retry disconnecting.

Google application credentials, provider refresh tokens, consent scopes and
Google token exchange belong to the private control plane. Do not set Google
client IDs or client secrets on harness instances. The runtime has no embedded
operator credentials, control-plane hostname, or native Google OAuth callback.
Redirects from the credential broker are refused; credentials are kept in the
harness's private credential store and registered for log redaction.

## Fresh start

Old direct Google grants and pasted tokens cannot authenticate Workspace tools.
Reconnect existing Workspace entries after installing the new app and runtime.
The obsolete umbrella Google entry and the pasted-token Ads, Ad Manager and
Analytics entries have been removed. Those products need dedicated control-plane
support before returning to the catalog. Dotobot account login is separate and
is not changed by this migration.

Gmail keeps its existing IMAP/SMTP tools with OAuth access. Calendar, Drive,
Sheets and Docs use host-pinned JSON REST tools. Drive binary transfers are not
implemented by those JSON tools. Disconnecting does not delete Google content.

## Verification

Offline tests prove delegation, isolation, credential redaction, account naming,
legacy-token refusal and revocation. Actual Google consent, automatic renewal,
two-account isolation and bot operations must also be checked after deployment;
source tests do not establish live availability.
