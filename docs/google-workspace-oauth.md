# Google Workspace connections

Gmail, Google Calendar, Google Drive and Google Sheets use Google OAuth.
App passwords and pasted access tokens are no longer used for these connectors.
Old stored secrets do not count as connected and are never used as fallback.
Reconnect each account from Manage > Plugins after upgrading the server and app.

Google account sign-in and Workspace API access are separate grants. A verified
sign-in branding screen does not verify Gmail or Drive access.

## Server configuration

Register clients in the Google project that owns your consent screen:

- Desktop application: configure `GOOGLE_DESKTOP_CLIENT_ID` and, if issued,
  `GOOGLE_DESKTOP_CLIENT_SECRET` in the server environment. The Mac app receives
  the code at `http://127.0.0.1:18765/callback` and relays it to its server.
- iOS application: bundle ID `com.dotobot.ios`; configure `GOOGLE_IOS_CLIENT_ID`.
  The app uses an authentication session with `com.dotobot.ios:/oauth/google`.
- An optional web client requires `GOOGLE_WEB_CLIENT_ID`,
  `GOOGLE_WEB_CLIENT_SECRET` and the exact HTTPS server callback registered.
  Native connections do not require a web client.

Keep client secrets on the server, outside source control. Supplying an explicit
client ID through the API uses only its explicitly supplied secret; it does not
borrow the server's secret for a different client. No service-account key or
Google account password is needed. Without registered client configuration the
server returns an actionable setup error, not a working connection.

Enable the Calendar, Drive and Sheets APIs. Gmail uses IMAP/SMTP with XOAUTH2.
Each connector requests its own scope, offline access and consent:

| Connector | Scope |
| --- | --- |
| Gmail | `https://mail.google.com/` |
| Calendar | `https://www.googleapis.com/auth/calendar` |
| Drive | `https://www.googleapis.com/auth/drive` |
| Sheets | `https://www.googleapis.com/auth/spreadsheets` |

The Gmail scope grants full mail access because its existing IMAP/SMTP tools
need it. The Drive scope covers existing files, not only app-created files.
Google's sensitive/restricted-scope verification and any applicable security
assessment must be completed for public use. Do not treat successful account
login or local tests as proof that these API scopes have been approved.

## Connection lifecycle

The server creates a short-lived, single-use state and PKCE S256 challenge.
Google returns the code to the native app; the app verifies state and relays the
code to the same server. The server exchanges it, requires the requested grant
and a refresh token, and stores account-specific credentials with mode 0600.
Tokens refresh before expiry. A failed refresh asks for reconnection rather than
falling back to an old password. Disconnect removes the local grant and pending
flow; it does not revoke other applications or delete Google content. Google
Account permissions can also revoke the grant remotely.

For Gmail, enter the mailbox address belonging to the Google account selected
in consent. Each additional account has a separate record and refresh token.

## Acceptance

Automated tests cover consent parameters, PKCE, state replay, incomplete grants,
refresh, account separation, XOAUTH2 and ignoring old credentials. Before
release, verify actual consent and a read from each service on a disposable
server, then refresh, disconnect and reconnect. Verify both Mac and iPhone
callbacks. Build/test success alone is not live Google authorization evidence.

Google Ads, Ad Manager and Analytics are outside this Workspace change.
