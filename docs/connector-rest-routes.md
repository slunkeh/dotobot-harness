# REST connector implementation progress

Scope: implement the 144 catalogue connectors identified with missing REST hosts.
This is an implementation ledger, not a claim of live-account verification.

Sixty-two routes now have offline request-contract coverage through the real connector
registry and credential store. Tests assert the outbound origin, version prefix,
credential header, query and JSON body. Credentials are never followed through
HTTP redirects. Production authenticated reads and writes remain unverified for these
sixty-two routes. The remaining 82 connectors still need provider research and code.

## Implemented routes

Paths passed to tools are relative to the configured base. API credentials belong
in the connector secret store, never tool arguments or configuration fields.
ActiveCampaign additionally needs `api_domain` from its account settings.
GetResponse currently covers SMB accounts; MAX routing remains to be implemented.
CloudConvert currently uses its production automatic-region API, not its sandbox.

| Connector | Provider reference | Setup |
|---|---|---|
| `emailverify_io` | [Provider documentation](https://www.emailverify.io/api/docs) | Store the account API key. GET /v2/check-account-balance reads credits. POST /v1/validate-batch takes title and email_batch containing address objects, up to 5000. The stored key is inserted in GET queries or POST JSON; never pass it in tool arguments. Poll /v1/get-result-bulk-verification-task/ with task_id. Verification consumes credits. |
| `acumbamail` | [Provider documentation](https://acumbamail.com/apidoc/) | Store the auth token from My account > Preferences. Dotobot inserts auth_token into GET queries or POST form data from the secret store. Use function paths with trailing slashes, such as /getLists/. JSON is the default response format. Pass POST parameters as a body object; nested fields are form-encoded with bracket notation. Some GET functions can modify data too: select functions carefully. Do not include auth_token in tool arguments. |
| `leaddyno` | [Provider documentation](https://support.leaddyno.com/hc/en-us/articles/21508238902173-Getting-Started-with-LeadDyno-API-Tracking) | Store the LeadDyno private API key from Account > Profile, not the public tracking key. It is sent in the documented key header. GET /visitors reads visitor records. POST /visitors takes a url field; body objects are form-encoded. Lead and purchase writes can change affiliate attribution; use only test data when checking writes. |
| `emaillistverify` | [Provider documentation](https://api.emaillistverify.com/api-doc) | Store an EmailListVerify API key, sent in x-api-key. GET /credits reads balances without verifying an email. POST /emailJobs accepts JSON email and optional quality; poll /emailJobs/JOB_ID for completion. Verification operations consume credits. Multipart list uploads and binary downloads are not supported by the generic JSON tool. |
| `giantcampaign` | [Provider documentation](https://giantcampaign.com/developers/) | Store the GiantCampaign API token. Dotobot adds the required api_token query parameter from the secret store; do not include it in tool arguments. GET /lists or /campaigns reads resources. The documented POST endpoints also pass parameters in the URL query: supply those non-secret parameters in path, using URL encoding. JSON-body acceptance is not verified. Sending campaigns or subscriber actions may trigger email. |
| `clickfunnels` | [Provider documentation](https://developers.myclickfunnels.com/docs/getting-started) | Store a ClickFunnels 2.0 platform application API access token, used as Bearer. Set subdomain to accounts for GET /teams and /teams/TEAM_ID/workspaces; use the actual workspace subdomain for /workspaces/WORKSPACE_ID/contacts and workspace writes. Enter only the subdomain, without scheme or .myclickfunnels.com. Use separate connector records if both scopes are needed. JSON bodies are supported and Dotobot supplies the required User-Agent. Tokens are team-wide. OAuth consent and refresh are not handled here. |
| `adtraction` | [Provider documentation](https://apidocs.adtraction.net/nextgen/) | Store the API token from Adtraction Account > Settings > API. Uses X-Token authentication and JSON bodies. Include the API version in each path: GET /v2/partner/markets/ or POST /v3/partner/programs/ with market in the JSON body. Both v2 and v3 share the configured host; prefer v3 replacements for deprecated v2 endpoints. Keep documented trailing slashes. Pagination starts at page 0. |
| `easypromos` | [Provider documentation](https://easypromos-apiref.redoc.ly/) | Store an access token from the Easypromos account Utilities menu. Uses Bearer authentication; White Label or Corporate plan required. GET /promotions lists promotions; use paging.next_cursor for further pages. POST requests use JSON. Some participation operations also require a participant login token in the body. Legacy v1 endpoints are retired. |
| `botconversa` | [Provider documentation](https://backend.botconversa.com.br/swagger/) | Store a BotConversa API key. Authentication uses API-KEY. Keep endpoint trailing slashes, for example GET /tags/ or /flows/. JSON writes are supported. POST /subscriber/ requires has_opt_in_whatsapp=true and actual contact consent. Messaging and flow endpoints can contact subscribers; adding the route does not authorize outreach. |
| `benchmark_email` | [Provider documentation](https://benchmarkemail.github.io/RESTful-API-v3/) | Store a Benchmark Email API token. Uses AuthToken and application/json headers, including GET. Read lists with GET /Contact/ and query SearchFilter. Create a list with POST /Contact and body Data containing Name and Description. Response.Status must be 1; HTTP 200 alone can contain an application error. This is REST v3, not the legacy XML API. |
| `klenty` | [Provider documentation](https://support.klenty.com/en/articles/3197537-getting-started-with-klenty-api) | Store the API key from Klenty Settings > Integrations. Authentication uses x-API-key. Paths include the account email, URL-encoded as a path segment: GET /user/ACCOUNT_EMAIL/lists. POST /user/ACCOUNT_EMAIL/prospects accepts a JSON object with Email and FirstName. Inspect response status for operation success. This route supports individual JSON objects; bulk prospect arrays are not supported. Starting cadences can send outreach. |
| `humanitix` | [Provider documentation](https://api.humanitix.com/v1/documentation/json) | Store the public API key from Humanitix Account > Advanced. Authentication uses x-api-key. Production paths are relative to /v1, for example GET /events with query page=1, or /tags. The current API reference also lists bodyless POST /events/EVENT_ID/tickets/TICKET_ID/check-in and check-out. Event creation, event updates and ticket transfers require additional provider permission. Old console API keys do not work. Staging and optional location override headers are not configured. |
| `campayn` | [Provider documentation](https://github.com/nebojsac/Campayn-API) | Store an API key from the Campayn Account section. Authentication uses Authorization: TRUEREST apikey=KEY. GET /lists.json reads lists. JSON writes such as POST /lists/LIST_ID/contacts.json add contacts; inspect success in the response. Keep the .json endpoint suffix. |
| `campaign_cleaner` | [Provider documentation](https://docs.campaigncleaner.com/api-reference/endpoint/get-credits) | Store a Campaign Cleaner API key. Authentication uses X-CC-API-Key. GET /get_credits reads the credit balance. POST /send_campaign accepts a send_campaign object with campaign_html and campaign_name; this submits analysis and consumes credits. Poll the campaign status before retrieving results. JSON endpoints are supported; binary PDF responses are not. |
| `abyssale` | [Provider documentation](https://developers.abyssale.com/rest-api/quickstart) | Store a workspace API key. REST paths are relative to https://api.abyssale.com; for example GET /designs. Authentication uses x-api-key. |
| `activecampaign` | [Provider documentation](https://developers.activecampaign.com/reference/authentication) | Set api_domain to the host from Settings > Developer API URL, without https:// or /api/3. Do not guess the region. Store the API token. Paths are relative to /api/3, for example /users/me; authentication uses Api-Token. |
| `attentive` | [Provider documentation](https://docs.attentive.com/docs/authentication) | Store the private application API key. Paths are relative to https://api.attentivemobile.com/v1; GET /subscriptions needs an email or phone query. Authentication uses Bearer. |
| `callrail` | [Provider documentation](https://apidocs.callrail.com/) | Store a CallRail API key. Paths are relative to https://api.callrail.com/v3, for example GET /a.json. Authentication uses Authorization: Token token=KEY. |
| `cloud_convert` | [Provider documentation](https://cloudconvert.com/docs/getting-started/introduction) | Store a scoped CloudConvert API key. Uses the production https://api.cloudconvert.com/v2 API; GET /users/me requires user.read. Paths omit /v2. Authentication uses Bearer. |
| `doppler` | [Provider documentation](https://restapi.fromdoppler.com/docs/gettingstarted) | Store a Doppler Email Marketing API key from Control Panel > Advanced Preferences. Paths are relative to https://restapi.fromdoppler.com, for example /accounts/ACCOUNT_EMAIL/lists. Authentication uses token KEY. This is not the Doppler secrets product. |
| `getresponse` | [Provider documentation](https://apidocs.getresponse.com/v3/authentication) | Store a GetResponse SMB API key. Uses https://api.getresponse.com/v3 and X-Auth-Token: api-key KEY. GET /accounts checks access. GetResponse MAX accounts require a different host and X-Domain and are not covered by this route. |
| `360nrs` | [Provider documentation](https://apidocs.360nrs.com/) | Store username:apiPassword as the secret, using the API password, not the platform login password. Allow the server IP in 360NRS settings. Paths are relative to https://dashboard.360nrs.com/api/rest; HTTP Basic authentication is used. |
| `4dem` | [Provider documentation](https://api.4dem.it/open-api) | Store the 4Dem API key. The connector exchanges it at /authenticate for a bearer token before every request. Paths are relative to https://api.4dem.it, for example /addressbook/. Dedicated and partner API-channel hosts are not covered. |
| `active_trail` | [Provider documentation](https://webapi.mymarketing.co.il/api/docs/Guides) | Store the access token from Settings > API apps. The token is sent unchanged in Authorization. Paths are relative to https://webapi.mymarketing.co.il/api, for example /groups. Check token expiry and allowed IPs. |
| `campaign_monitor` | [Provider documentation](https://www.campaignmonitor.com/api/v3-3/getting-started/) | Store only the Campaign Monitor API key. HTTP Basic uses it as username with an empty password. Paths are relative to https://api.createsend.com/api/v3.3; use .json endpoints such as /clients.json. |
| `drip` | [Provider documentation](https://developer.drip.com/) | Store the personal API token. HTTP Basic uses it as username with an empty password. Paths are relative to https://api.getdrip.com and include their version, for example /v2/accounts or /v3/ACCOUNT_ID/shopper_activity/order/batch. |
| `bigmailer` | [Provider documentation](https://docs.bigmailer.io/docs/getting-started-api) | Store a BigMailer API key. Paths are relative to https://api.bigmailer.io/v1, for example /me. Authentication uses X-API-Key and JSON bodies. |
| `cardly` | [Provider documentation](https://api.card.ly/v2/docs) | Store a Cardly test_ or live_ API key; use test_ keys to avoid order mutations while testing. Paths are relative to https://api.card.ly/v2, for example /art. Authentication uses API-Key and bodies use text/json as required by Cardly. |
| `dropcontact` | [Provider documentation](https://developer.dropcontact.com/) | Store a Dropcontact access token. Paths are relative to https://api.dropcontact.com/v1/enrich; use /all for enrichment and /webhook for callback configuration. Authentication uses X-Access-Token. |
| `dynapictures` | [Provider documentation](https://dynapictures.com/docs/) | Store a DynaPictures API key. Paths are relative to https://api.dynapictures.com, for example /workspaces or /designs/TEMPLATE_ID. Authentication uses Bearer. |
| `egoi` | [Provider documentation](https://developers.e-goi.com/api/v3/) | Store the E-goi API key from account settings. Paths are relative to https://api.egoiapp.com, for example /my-account; do not add /v3. Authentication uses Apikey. |
| `email_on_acid` | [Provider documentation](https://api.emailonacid.com/docs/latest) | Store api_key:account_password as the secret for HTTP Basic authentication. Paths are relative to https://api.emailonacid.com/v5, for example /auth. The documented public sandbox uses sandbox:sandbox. |
| `fomo` | [Provider documentation](https://github.com/usefomo/fomo-python-sdk/blob/master/Fomo/fomo.py) | Store the site Auth Token from Settings > Site. Paths are relative to https://api.fomo.com/api/v1, for example /applications/me/events. Authentication uses Authorization: Token KEY. API access normally requires a paid plan. |
| `growsurf` | [Provider documentation](https://docs.growsurf.com/developer-tools/rest-api) | Store a GrowSurf API key. Paths are relative to https://api.growsurf.com/v2 and include the program ID, for example /campaign/PROGRAM_ID. Authentication uses Bearer; account plan eligibility is required. |
| `instasent` | [Provider documentation](https://docs.instasent.com/developers/product-api/authentication/) | Store a scoped Product API token. Paths are relative to https://api.instasent.com/v1, for example /project/PROJECT_UID. Include the real project UID in resource paths. Authentication uses Bearer. This route covers the Product API, not the separate transactional SMS API. |
| `acelle_mail` | [Provider documentation](https://acellesend.com/rest-api) | Set instance_domain to the HTTPS hostname of your Acelle Mail installation, without a scheme or path. Store the API token from My Profile > API and Authentication. Paths are relative to /api/v1, for example /me. Authentication uses Bearer. Installations under a URL subdirectory are not covered. |
| `emailable` | [Provider documentation](https://emailable.com/docs/api/authentication/) | Store an Emailable private API key or OAuth access token. Paths are relative to https://api.emailable.com/v1, for example /account. Authentication uses Bearer; public keys only allow verification. Test keys simulate verification without using credits. |
| `joggai` | [Provider documentation](https://docs.jogg.ai/api-reference/v2/Webhook/ListWebhookEndpoints) | Store the JoggAI dashboard API key. Uses x-api-key with paths relative to /v2, for example GET /endpoints and POST /endpoint. Inspect the JSON code as well as HTTP status: only code 0 denotes success. JSON requests are supported; multipart uploads are not. |
| `copicake` | [Provider documentation](https://docs.copicake.com/api/v1-image-get) | Store a Copicake API key. Paths are relative to /v1. GET /image/get requires the rendering id query parameter. POST /image/create accepts template_id, changes and options as JSON. Authentication uses Bearer. |
| `emailoctopus` | [Provider documentation](https://emailoctopus.com/api-documentation/v2) | Store an EmailOctopus API key. Uses API v2 with Bearer authentication at https://api.emailoctopus.com, without a /v2 path prefix. GET /lists reads lists. Pass starting_after for cursor pagination. Legacy v1 query-key authentication is not used. |
| `beamer` | [Provider documentation](https://www.getbeamer.com/help/how-to-use-single-user-notifications) | Store a Beamer API key from Settings > API. Paths are relative to /v0, for example GET /posts or POST /posts with a JSON body. Authentication uses Beamer-Api-Key. Key permissions control read and write access. |
| `convertkit` | [Provider documentation](https://developers.kit.com/api-reference/authentication) | ConvertKit is now Kit. Store a V4 API key from Developer settings for personal account automation. Authentication uses X-Kit-Api-Key; paths are relative to /v4, for example GET /account. Legacy V3 keys are incompatible. Some endpoints, including bulk and purchase creation, require OAuth and are not covered by this key route. |
| `esputnik` | [Provider documentation](https://docs.esputnik.com/reference/getting-started-with-your-api) | Store a secret in username:API_KEY format, using any nonempty username and the API key as the password. HTTP Basic authentication is used. Paths include their version, for example GET /v1/account/info. Most writes are asynchronous: an HTTP success means acceptance, not completed processing. |
| `cyberimpact` | [Provider documentation](https://api.cyberimpact.com/docs) | Store the JWT API token from Developers > API tokens. Authentication uses Bearer. Paths are relative to https://api.cyberimpact.com; GET /groups reads groups and POST /groups accepts JSON with title and isPublic. Use page and limit for pagination. |
| `eventbrite` | [Provider documentation](https://www.eventbrite.com/platform/new/api) | Store your Eventbrite personal OAuth token, not the application client secret. Authentication uses Bearer. Paths are relative to /v3 and normally end with a slash, for example GET /users/me/. JSON writes use the endpoint schema. Access depends on the token owner and organization permissions; OAuth authorization for other users is not performed by this stored-token route. |
| `laposta` | [Provider documentation](https://api.laposta.nl/doc/index.en.php) | Store the Laposta API key alone; HTTP Basic uses it as the username with an empty password. Paths are relative to https://api.laposta.org/v2, for example GET /list. Regular writes use form fields with bracket notation for nested objects; POST /list/LIST_ID/members uses JSON for bulk synchronization. Pass the body as an object; Dotobot selects the encoding. Bulk synchronization requires a paid account. |
| `google_calendar` | [Provider documentation](https://developers.google.com/workspace/calendar/api/v3/reference) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /users/me/calendarList lists calendars; use a token with calendar.calendarlist.readonly or another scope allowed by that endpoint. Writes need the corresponding calendar scope. Paths omit /calendar/v3. |
| `google_drive` | [Provider documentation](https://developers.google.com/workspace/drive/api/reference/rest/v3) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /files lists file metadata; drive.metadata.readonly is sufficient for that read. JSON metadata writes need an appropriate write scope. Paths omit /drive/v3. Binary and multipart uploads or downloads are not supported by these text tools. |
| `google_sheets` | [Provider documentation](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /spreadsheets/SPREADSHEET_ID reads a spreadsheet; spreadsheets.readonly is sufficient for reads. Writes require spreadsheets or another supported write scope. Paths omit /v4. Use fields and ranges to limit large responses. |
| `microsoft_excel` | [Provider documentation](https://learn.microsoft.com/en-us/graph/api/workbook-list-worksheets?view=graph-rest-1.0) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /me/drive/items/ITEM_ID/workbook/worksheets lists worksheets. Use a delegated Graph token with Files.ReadWrite; application-only tokens are unsupported for this method. Calls are sessionless: workbook changes persist. Workbook-Session-Id and file uploads are not supported. Uses the global Graph cloud; paths omit /v1.0. |
| `microsoft_outlook` | [Provider documentation](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /me/messages lists messages with delegated Mail.ReadBasic for basic properties; bodies need Mail.Read. Application tokens use /users/USER_ID/messages with application permissions. Mail writes need corresponding permissions. Uses the global Graph cloud; paths omit /v1.0. |
| `microsoft_teams` | [Provider documentation](https://learn.microsoft.com/en-us/graph/api/user-list-joinedteams?view=graph-rest-1.0) | Store a current OAuth access token as the connector secret, not an API key, refresh token or client secret. Authentication uses Bearer. This route does not run OAuth consent or refresh expired tokens; replace the stored access token when it expires. GET /me/joinedTeams requires delegated Team.ReadBasic.All with a work or school account. Personal accounts are unsupported. Application tokens use /users/USER_ID/joinedTeams. Other operations require their own permissions. Uses the global Graph cloud; paths omit /v1.0. |
| `cleverreach` | [Provider documentation](https://developers.cleverreach.com/docs/api-categories/introduction/) | Store a current CleverReach OAuth access token, not the client secret. Authentication uses Bearer. Paths are relative to /v3, for example GET /groups or POST /groups with a JSON name field. OAuth consent and expired-token refresh are not performed by this stored-token route. |
| `constant_contact` | [Provider documentation](https://developer.constantcontact.com/api_guide/getting-started/v3-technical-overview) | Store a current Constant Contact V3 OAuth access token. Authentication uses Bearer; paths are relative to /v3, for example GET /contacts. Writes use JSON and require corresponding scopes. OAuth consent and expired-token refresh are not performed by this stored-token route. |
| `lawmatics` | [Provider documentation](https://help.lawmatics.com/en/articles/15939403-api-authentication-oauth2-setup-guide) | Store a Lawmatics OAuth access token obtained through a developer app. Authentication uses Bearer; paths are relative to /v1, for example GET /users/me. Lawmatics documents non-expiring tokens with no scopes and full account CRUD access; revoke through integration settings. This route does not perform OAuth consent. JSON bodies are supported, not multipart uploads. |
| `infusionsoft` | [Provider documentation](https://developer.infusionsoft.com/postman-quick-start/) | Infusionsoft is now Keap. Store a Personal Access Token or Service Account Key. Authentication uses X-Keap-API-Key. Paths are relative to /crm/rest/v1, for example GET /contacts; JSON bodies follow the REST v1 schema. This route does not use legacy XML-RPC keys or OAuth bearer tokens. REST v2 is not covered by this v1 base. |
| `ecologi` | [Provider documentation](https://docs.ecologi.com/) | Store the Ecologi Impact API key; authentication uses Bearer. Paths are relative to https://public.ecologi.com. POST /impact/trees takes a JSON number and test flag. Set test to true for non-billable test requests; live impact purchases are billed. Public reporting uses GET /users/USERNAME/trees and does not require authentication at the provider, although this connector currently requires a stored key. Idempotency-Key headers are not exposed; do not automatically retry purchase requests. |
| `greenspark` | [Provider documentation](https://docs.getgreenspark.com/reference/authentication) | Store a Greenspark API key, not a Widget key. Authentication uses X-API-KEY. Paths are relative to the production /v1 API, for example GET /projects. Impact writes take JSON and may incur charges. This route uses production; the separate demo and sandbox environments are not configured. Plan eligibility and key permissions apply. |
| `enormail` | [Provider documentation](https://developer.enormail.eu/) | Store the Enormail API key alone. HTTP Basic uses it as username with an empty password. Paths are relative to /api/1.0, for example GET /account.json. Keep the .json endpoint suffix. POST and PUT bodies are form encoded, including bracket notation for nested fields. Pass body as an object. DELETE parameters belong in the path query string. |
| `gist` | [Provider documentation](https://developers.getgist.com/api/) | Store the Gist workspace API key from Integration Settings. Authentication uses Bearer. Paths are relative to https://api.getgist.com, for example GET /contacts. Writes use JSON. This is Gist customer messaging, not GitHub Gists or gist.ai. |
| `cometly` | [Provider documentation](https://docs.cometly.com/introduction/authentication) | Store the Cometly API integration key. Authentication uses Bearer; Accept and Content-Type are application/json, including GET requests. Paths are relative to /public-api/v1. GET /events requires start_date and end_date in YYYY-MM-DD HH:MM:SS format in the space timezone. This is the REST API, not the separate MCP endpoint. |
| `crowdpower` | [Provider documentation](https://docs.crowdpower.io/getting-started/beacon-api) | Store a CrowdPower project secret or application key. Authentication uses Bearer. Uses the Beacon ingestion API, for example POST /customers with user_id and customer fields as JSON, or /customers/bulk with a customers array. This scope provides ingestion, not a documented customer-list GET. Inspect the response success and code fields. Ingestion can trigger configured marketing automations. |

ActiveCampaign host selection: [official base URL guidance](https://developers.activecampaign.com/reference/url).

## Verification evidence

2026-09-20: all seven new route cases fail when their route metadata is removed.
144 focused tests passed across request routes, generic connectors,
redaction, connector tools and mentions. Six sequential network probes using a
synthetic invalid key reached Abyssale, Attentive, CallRail, CloudConvert, Doppler Email Marketing
and GetResponse through the actual generic connector and each returned HTTP 401.
This demonstrates route reachability only; no authenticated functionality is
claimed. ActiveCampaign cannot be probed without a real account-specific host.

Full suite: 3298 passed, 2 skipped, 7 failed on the first run. Four export failures
were fixed by permitting this reviewed public document; all 23 export tests then
passed. The short temporary-path rerun also fixes one Unix socket path-length
failure. The remaining CDP listener and machine-supervisor display tests fail
identically on unchanged baseline `c33bb96` in this macOS environment. The full
suite is therefore not recorded as green. Ruff and the CLI help check pass.

Second batch adds seven routes and tests 4Dem token exchange, invalid-token refusal,
authentication failure without a resource write, and token rotation between
calls. These checks use synthetic credentials and do not verify real account access.

Second-batch focused validation: 186 tests pass across REST routes, generic connectors,
redaction, connector catalogue/tools, mentions and public export. Ruff passes.

Second-batch invalid-credential probes: 360NRS, ActiveTrail, Campaign Monitor,
Drip and Cardly returned HTTP 401; 4Dem rejected its authentication exchange with
HTTP 401; BigMailer returned HTTP 400 with an invalid API-key error. No authenticated
account access is claimed. Cardly JSON content-type handling is also covered.

Third batch adds nine routes: Acelle Mail, Dropcontact, DynaPictures, E-goi,
Email on Acid, Emailable, Fomo, GrowSurf and Instasent Product API. Current focused
validation: 200 tests pass; Ruff passes. Acelle host configuration is tested for
missing values, full URLs, path injection and userinfo injection before network I/O.

Live evidence for the third batch:

- Email on Acid: documented public sandbox credentials authenticate successfully
  at `/auth` (HTTP 200, success true), and the bound registry tool reads
  `/spam/clients` (HTTP 200, 14 entries). This validates sandbox auth and a read,
  not production access or the email rendering workflow.
- Dropcontact, DynaPictures and E-goi return HTTP 401 with an invalid key.
- Fomo returns HTTP 401, "Token is required", for the deliberately invalid token;
  its header format is checked against the provider-owned Python SDK.
- GrowSurf and Emailable return HTTP 403 identifying the invalid key.
- Instasent returns HTTP 403, "No token could be found", for the invalid token;
  its bearer format is checked against the Product API documentation.
- Acelle Mail needs an operator-supplied installation host and token; no live
  instance was used.

## Full 144-connector ledger

| Connector | Implementation status |
|---|---|
| `360nrs` | Route and offline request tests added; live verification pending |
| `4dem` | Route and offline request tests added; live verification pending |
| `abyssale` | Route and offline request tests added; live verification pending |
| `acelle_mail` | Route and offline request tests added; live verification pending |
| `activecampaign` | Route and offline request tests added; live verification pending |
| `active_trail` | Route and offline request tests added; live verification pending |
| `acumbamail` | Implemented; request contracts pass; authenticated account verification pending |
| `acymailing` | Pending provider research and implementation |
| `add_to_calendar_pro` | Pending provider research and implementation |
| `adhook` | Pending provider research and implementation |
| `adrapid` | Pending provider research and implementation |
| `adroll` | Pending provider research and implementation |
| `adtraction` | Implemented; request contracts pass; authenticated account verification pending |
| `aimtell` | Pending provider research and implementation |
| `airship` | Pending provider research and implementation |
| `apexverify` | Pending provider research and implementation |
| `appsflyer` | Pending provider research and implementation |
| `arpoone` | Pending provider research and implementation |
| `asters` | Pending provider research and implementation |
| `attentive` | Route and offline request tests added; live verification pending |
| `autoklose` | Pending provider research and implementation |
| `automizy` | Pending provider research and implementation |
| `beamer` | Implemented; request-contract tests pass; production account verification pending |
| `benchmark_email` | Implemented; request contracts pass; authenticated account verification pending |
| `bigmailer` | Route and offline request tests added; live verification pending |
| `botconversa` | Implemented; request contracts pass; authenticated account verification pending |
| `brandmentions` | Pending provider research and implementation |
| `builderall_mailingboss` | Pending provider research and implementation |
| `buysellads` | Pending provider research and implementation |
| `callpage` | Pending provider research and implementation |
| `callrail` | Route and offline request tests added; live verification pending |
| `campaign_cleaner` | Implemented; request contracts pass; authenticated account verification pending |
| `campaign_monitor` | Route and offline request tests added; live verification pending |
| `campaignhq` | Pending provider research and implementation |
| `campayn` | Implemented; request contracts pass; authenticated account verification pending |
| `cardly` | Route and offline request tests added; live verification pending |
| `catch_all_verifier` | Pending provider research and implementation |
| `chatrace` | Pending provider research and implementation |
| `cleverreach` | Implemented; request-contract tests pass; production account verification pending |
| `clevertap` | Pending provider research and implementation |
| `clickfunnels` | Implemented; request contracts pass; authenticated account verification pending |
| `cloud_convert` | Route and offline request tests added; live verification pending |
| `cometly` | Implemented; request contracts pass; authenticated account verification pending |
| `constant_contact` | Implemented; request-contract tests pass; production account verification pending |
| `contentdrips` | Pending provider research and implementation |
| `convertkit` | Implemented; request-contract tests pass; production account verification pending |
| `copicake` | Implemented; request-contract tests pass; production account verification pending |
| `coupontools` | Pending provider research and implementation |
| `crowdpower` | Implemented; request contracts pass; authenticated account verification pending |
| `curated` | Pending provider research and implementation |
| `cyberimpact` | Implemented; request-contract tests pass; production account verification pending |
| `demandbase` | Pending provider research and implementation |
| `demio` | Pending provider research and implementation |
| `discourse` | Pending provider research and implementation |
| `docupost` | Pending provider research and implementation |
| `doppler` | Route and offline request tests added; live verification pending |
| `dribbble` | Pending provider research and implementation |
| `drip` | Route and offline request tests added; live verification pending |
| `dripcel` | Pending provider research and implementation |
| `dropcontact` | Route and offline request tests added; live verification pending |
| `dux_soup` | Pending provider research and implementation |
| `dynamic_content_snippet` | Pending provider research and implementation |
| `dynapictures` | Route and offline request tests added; live verification pending |
| `egoi` | Route and offline request tests added; live verification pending |
| `easypromos` | Implemented; request contracts pass; authenticated account verification pending |
| `easysendy` | Pending provider research and implementation |
| `echtpost_postcards` | Pending provider research and implementation |
| `ecologi` | Implemented; request contracts pass; authenticated account verification pending |
| `email_on_acid` | Route and offline tests added; public sandbox authentication and read passed; production verification pending |
| `emailable` | Route and offline request tests added; live verification pending |
| `emailchef` | Pending provider research and implementation |
| `emaillistverify` | Implemented; request contracts pass; authenticated account verification pending |
| `emailoctopus` | Implemented; request-contract tests pass; production account verification pending |
| `emelia` | Pending provider research and implementation |
| `encharge` | Pending provider research and implementation |
| `endorsal` | Pending provider research and implementation |
| `engage` | Pending provider research and implementation |
| `enginemailer` | Pending provider research and implementation |
| `enormail` | Implemented; request contracts pass; authenticated account verification pending |
| `esputnik` | Implemented; request-contract tests pass; production account verification pending |
| `eventbrite` | Implemented; request-contract tests pass; production account verification pending |
| `everwebinar` | Pending provider research and implementation |
| `exact_mails` | Pending provider research and implementation |
| `facebook` | Pending provider research and implementation |
| `feedblitz` | Pending provider research and implementation |
| `flexmail` | Pending provider research and implementation |
| `flippingbook` | Pending provider research and implementation |
| `fomo` | Route and offline request tests added; live verification pending |
| `freshmarketer` | Pending provider research and implementation |
| `funnelcockpit` | Pending provider research and implementation |
| `getemails` | Pending provider research and implementation |
| `getresponse` | Route and offline request tests added; live verification pending |
| `getswift` | Pending provider research and implementation |
| `giantcampaign` | Implemented; request contracts pass; authenticated account verification pending |
| `gist` | Implemented; request contracts pass; authenticated account verification pending |
| `gitter` | Pending provider research and implementation |
| `gobio_link` | Pending provider research and implementation |
| `goodbits` | Pending provider research and implementation |
| `google_ad_manager` | Pending provider research and implementation |
| `google_ads` | Pending provider research and implementation |
| `google_analytics` | Pending provider research and implementation |
| `google_calendar` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `google_drive` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `google_sheets` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `gosquared` | Pending provider research and implementation |
| `gozen_growth` | Pending provider research and implementation |
| `grade_us` | Pending provider research and implementation |
| `greenspark` | Implemented; request contracts pass; authenticated account verification pending |
| `growsurf` | Route and offline request tests added; live verification pending |
| `herobot` | Pending provider research and implementation |
| `heysummit` | Pending provider research and implementation |
| `hippo_video` | Pending provider research and implementation |
| `humanitix` | Implemented; request contracts pass; authenticated account verification pending |
| `hypeauditor` | Pending provider research and implementation |
| `hyperise` | Pending: official API support pages currently fail TLS certificate validation; host and authentication still require verification |
| `icontact` | Pending provider research and implementation |
| `impression` | Pending provider research and implementation |
| `indiefunnels` | Pending provider research and implementation |
| `infusionsoft` | Implemented; request-contract tests pass; production account verification pending |
| `inksprout` | Pending provider research and implementation |
| `instabot` | Pending provider research and implementation |
| `instagram` | Pending provider research and implementation |
| `instasent` | Route and offline request tests added; live verification pending |
| `jellyreach` | Pending provider research and implementation |
| `joggai` | Implemented; request-contract tests pass; production account verification pending |
| `jvzoo` | Pending provider research and implementation |
| `kartra` | Pending provider research and implementation |
| `kickofflabs` | Pending provider research and implementation |
| `kingsumo` | Pending provider research and implementation |
| `klenty` | Implemented; request contracts pass; authenticated account verification pending |
| `kyvio` | Pending provider research and implementation |
| `lagrowthmachine` | Pending provider research and implementation |
| `lahar` | Pending provider research and implementation |
| `laposta` | Implemented; documented sandbox list read HTTP 200 (truncated); production verification pending |
| `lawmatics` | Implemented; request-contract tests pass; production account verification pending |
| `lead_identity_check` | Pending provider research and implementation |
| `leaddyno` | Implemented; request contracts pass; authenticated account verification pending |
| `leadoku` | Pending provider research and implementation |
| `leadpops` | Pending provider research and implementation |
| `linkedin` | Pending provider research and implementation |
| `microsoft_excel` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `microsoft_outlook` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `microsoft_teams` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |

### Fourth batch: JoggAI, Copicake and EmailOctopus

2026-09-20: three new route-contract tests failed with the original missing-host
metadata and pass after implementation. Added documented JSON write-path tests
for JoggAI webhook creation, Copicake image creation and EmailOctopus list creation.
206 focused tests pass; Ruff passes. Sequential invalid-key reads through the
real bound connector returned HTTP 401 from all three providers. JoggAI returned
code 10105, Copicake returned Unauthorized, and EmailOctopus rejected the token
format. These checks prove reachability, not account access or successful writes.
EmailOctopus uses its current v2 OpenAPI document, whose server URL has no version
path prefix. Production authenticated operations remain unverified.

### Fifth batch: Beamer, Kit and eSputnik

2026-09-20: all three new request-contract cases failed before their host and
authentication metadata was added, and pass afterward. 209 focused tests pass;
Ruff passes. Sequential reads through the bound connectors, using intentionally
invalid credentials, reached all three providers and returned HTTP 401. Beamer
and Kit explicitly reported invalid API keys; eSputnik reported Unauthorized.
No authenticated account operations or external writes were performed. Kit uses
V4 keys for personal automation; OAuth-only endpoints remain outside that key
route. eSputnik requires username:API_KEY in the secret store, not a bare key.

### Sixth batch: Cyberimpact and Eventbrite

2026-09-20: both route-contract cases failed before implementation and pass
afterward. 211 focused tests pass; Ruff passes. Bound-tool invalid-token reads
returned HTTP 401 from both providers. Cyberimpact rejected the JWT segment
count; Eventbrite returned INVALID_AUTH. These results prove endpoint
reachability only. No authenticated reads or writes are claimed. Cyberimpact
uses the official OpenAPI server and Bearer scheme. Eventbrite JSON request
format is documented in its [API basics](https://www.eventbrite.co.uk/platform/docs/api-basics),
and personal-token authentication in its [OAuth guide](https://www.eventbrite.com/platform/docs/app-oauth-flow).

Research still pending implementation: Laposta documents Basic API-key
authentication and form-encoded regular writes, with JSON for bulk member
operations. Its [reference](https://api.laposta.nl/doc/index.en.php) therefore
requires request-body selection before its route can be marked implemented.

### Seventh batch: Laposta body encoding

2026-09-20: four request-body cases failed before the Laposta implementation.
217 focused tests now pass; the new cases cover regular form fields, nested
objects and arrays, booleans, special characters, bulk JSON (including query
strings), GET without a body, and escaped credential substitution that cannot
inject another form field. Other providers retain their existing JSON behavior.

A bound GET /list using the provider's documented public testing key returned
HTTP 200 with list data. The connector truncated the response at its existing
12,000-character limit, so it cannot be treated as a complete JSON document or
a complete list inventory. The documentation's sample list ID returned HTTP 400
Unknown list; the sample ID appears stale. No live writes were performed and
production account operations remain unverified. The previously pending Laposta
body-format requirement is now implemented.

### Eighth batch: Google Workspace and Microsoft Graph

2026-09-20: six route cases failed before implementation and pass afterward.
Added Google Calendar, Drive and Sheets plus Microsoft Excel, Outlook and Teams
using stored OAuth access tokens. These are REST transports, not completed OAuth
consent/refresh integrations. Tokens must be renewed outside these routes.
230 focused tests pass; Ruff passes. Tests also cover documented JSON writes
and Sheets repeated range parameters. The repeated-parameter test failed before
the query encoder was changed to expand arrays; scalar queries remain covered.

Sequential bound-tool probes using an invalid access token returned HTTP 401
for all six routes. Google reported invalid authentication credentials; Microsoft
reported InvalidAuthenticationToken. No user credentials, account reads or writes
were used. Global Graph cloud, Excel sessionless behavior, Google text/JSON limits
and required permissions are recorded above. Production acceptance remains open.

Write contracts reference provider documentation for
[Calendar creation](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert),
[Drive metadata creation](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/create),
[Sheets creation](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/create),
[Excel worksheet creation](https://learn.microsoft.com/en-us/graph/api/worksheetcollection-add?view=graph-rest-1.0),
[Outlook folder creation](https://learn.microsoft.com/en-us/graph/api/user-post-mailfolders?view=graph-rest-1.0),
and [Teams channel creation](https://learn.microsoft.com/en-us/graph/api/channel-post?view=graph-rest-1.0).

### Ninth batch: CleverReach, Constant Contact, Lawmatics and Keap

2026-09-20: four route-contract cases failed before implementation and pass
afterward. 234 focused tests pass; Ruff passes. Sequential invalid-credential
reads through the bound connector returned HTTP 401 from all four providers.
CleverReach and Constant Contact reported Unauthorized, Lawmatics returned an
empty response, and Keap reported Invalid Access Token. These are reachability
checks only; account reads, writes and OAuth lifecycle behavior remain unverified.
Keap uses PAT/SAK header authentication on REST v1; the other three use stored
OAuth access tokens. Lawmatics documents non-expiring tokens, unlike the expiring
tokens used by CleverReach and Constant Contact.

Additional schema evidence: CleverReach publishes its JSON request schemas at
[the v3 OpenAPI endpoint](https://rest.cleverreach.com/v3/explorer/swagger.json).
Lawmatics documents its resource paths in
[Core Objects and Endpoints](https://help.lawmatics.com/en/articles/15939438-core-objects-endpoints-reference).

### Tenth batch: Ecologi and Greenspark

2026-09-20: both route cases failed before implementation and pass afterward.
237 focused tests pass; Ruff passes. Ecologi has an additional documented
JSON test-purchase request contract. A bound request with an intentionally
invalid key and test=true returned HTTP 401 (no user for the API key).
A bound Greenspark GET /projects returned HTTP 401 Unauthorized. No purchases
or account mutations were completed. Production account functionality is not
verified. Ecologi reporting is public but its current connector still expects
a stored key. Greenspark uses production; its documented demo/sandbox hosts
remain a separate configuration enhancement.

Greenspark host and JSON request evidence: [projects](https://docs.getgreenspark.com/reference/getprojects),
[impact creation](https://docs.getgreenspark.com/reference/createimpact), and
[environments](https://docs.getgreenspark.com/reference/environments).

Research pending implementation: Enormail's current official reference is
https://developer.enormail.eu/ (singular developer). It documents Basic
authentication using the API key as username and an empty password, at
https://api.enormail.eu/api/1.0. Request body encoding still needs verification.
The old Flexmail developer.flexmail.eu hostname does not resolve; its current
marketing API reference must be located before configuring that route.

### Eleventh batch: Enormail

2026-09-20: read, POST and PUT request tests failed before implementation and
pass afterward. 240 focused tests pass; Ruff passes. The official
[Enormail PHP transport](https://github.com/Enormail/enormail-php-api/blob/master/src/Enormail/Rest.php)
confirms the host and form-encoded writes; the reference confirms that the
Basic-auth password may be empty. Dotobot retains HTTPS certificate validation
and refuses redirects. GET /account.json through a disposable bound connector
with an invalid key returned HTTP 401 Authentication failed. No authenticated
account operation or external write was performed. The previously pending
Enormail body-format check is now resolved.

### Twelfth batch: Gist, Cometly and CrowdPower

2026-09-20: four new route/header cases failed before implementation and pass
afterward. 244 focused tests pass; Ruff passes. Gist and Cometly bound reads
with invalid credentials returned HTTP 401. CrowdPower Beacon /customers with
an invalid key and a synthetic user ID returned HTTP 401 with token.invalid;
no customer was accepted. No real account operations are verified.

Cometly requires Content-Type even on GET requests. Explicit catalogue content
types now apply to requests without bodies too (also matching Cardly's explicit
text/json format), while default GET requests remain without Content-Type.
CrowdPower has a documented ingestion scope, not a documented GET list API;
its request test follows [Identify Customer](https://docs.crowdpower.io/getting-started/beacon-api/identify-customer).
Cometly event reads require dates, as shown in
[List Events](https://docs.cometly.com/api-reference/endpoint/list-events).

## Campaign Cleaner and Campayn validation

Both routes failed their four new request-contract cases before implementation and
pass afterward. The focused suite now passes 248 tests; Ruff passes. Bound reads
using disposable stores and deliberately invalid keys returned HTTP 401 Invalid
API Key from Campaign Cleaner /get_credits and HTTP 403 Bad Authorization from
Campayn /lists.json. These prove endpoint reachability and rejection, not account
access. No live writes were performed.

Campaign Cleaner uses its [documented credit endpoint](https://docs.campaigncleaner.com/api-reference/endpoint/get-credits)
and [JSON analysis submission](https://docs.campaigncleaner.com/api-reference/endpoint/send-campaign).
Its linked openapi.json currently describes unrelated verification endpoints, so
route contracts follow the individual endpoint pages. Campaign analysis consumes
credits; binary PDF downloads are outside the generic JSON response support.

Campayn's [own integration page](https://www.campayn.com/api) links the
[official API repository](https://github.com/nebojsac/Campayn-API), whose subscribe
sample confirms the HTTPS origin, JSON body, and TRUEREST authorization prefix.
The contact-write test uses that documented request shape without creating a contact.

## Humanitix validation

The official help article links Humanitix Stoplight, which links the live
[OpenAPI reference](https://api.humanitix.com/v1/documentation/json). The reference
confirms the production host, x-api-key authentication, required page parameter
and JSON endpoints. Unlike the help article's read-only description, it now also
lists check-in/out and permission-gated event/transfer writes. Tests cover the
bound read, required page query and bodyless check-in request; no live writes ran.

The initial live GET /events returned HTTP 400 for missing page. Retrying with
page=1 returned HTTP 400 Invalid api key format provided for the deliberately
invalid key. This proves endpoint validation, not authenticated account access.
Three new cases pass, including two that failed before the route was added.
The focused suite passes 251 tests and Ruff passes.

## Klenty validation

Klenty's official [GET reference](https://support.klenty.com/en/articles/8193357-klenty-s-get-api-s)
and [POST reference](https://support.klenty.com/en/articles/8193937-klenty-s-post-apis)
confirm account-email path segments and JSON prospect objects. Two new request
cases failed before implementation and pass afterward, checking the encoded
account path, header and case-sensitive prospect fields. The focused suite passes
253 tests; Ruff passes. Bulk arrays remain unsupported by the generic request tool.

A bound GET /user/fixture%40example.com/lists using a disposable credential store
and invalid key returned HTTP 401 with reason invalidAPIKey. No prospects were
created and no cadences started. Authenticated account verification remains pending.

## Benchmark Email, BotConversa and Easypromos validation

Six new request cases failed without their routes and pass after implementation.
Coverage includes documented JSON list creation, subscriber creation and
participation-requirement requests. A seventh case checks Benchmark Email's
required JSON Content-Type on GET. The focused suite passes 260 tests; Ruff passes.

Bound reads with disposable stores and deliberately invalid credentials returned:

- Benchmark Email GET /Contact/: HTTP 401, Invalid/Missing AuthToken.
- BotConversa GET /tags/: HTTP 403, Api key is not valid.
- Easypromos GET /promotions: HTTP 403, invalid_token.

These are rejection checks, not account acceptance tests. No live lists,
subscribers, messages, or promotion entries were created.

Benchmark's [official examples](https://benchmarkemail.github.io/RESTful-API-v3/)
confirm its unversioned clientapi host and AuthToken; the interactive developer
site timed out during research. BotConversa's
[live schema](https://backend.botconversa.com.br/swagger/?format=openapi)
confirms JSON, API-KEY and the /api/v1/webhook prefix. Easypromos'
[current reference](https://easypromos-apiref.redoc.ly/) confirms v2 JSON and
Bearer authentication. Its White Label/Corporate requirement is recorded above.

## Adtraction validation

The [current unified reference](https://apidocs.adtraction.net/nextgen/) documents
v2 and v3 on api.adtraction.net with X-Token authentication. The configured base
omits the version so callers can use both, including v3 replacements for
deprecated v2 endpoints. Two new cases failed before implementation and pass
afterward. The focused suite passes 262 tests; Ruff passes.

Bound calls using an invalid token returned HTTP 401 Unauthorized access for
both GET /v2/partner/markets/ and POST /v3/partner/programs/ with market=SE.
The latter is a documented retrieval operation despite using POST. No account
data was changed. Authenticated provider access remains unverified.

## ClickFunnels validation

The [current getting-started guide](https://developers.myclickfunnels.com/docs/getting-started)
requires accounts.myclickfunnels.com for team queries and each workspace's
subdomain for workspace data. Both configurations now pass request contracts,
which failed before the route was added. Five malformed configuration cases
remain blocked before network access. Tests also check Dotobot's User-Agent.
The focused suite passes 269 tests; Ruff passes.

A bound GET /teams on the documented accounts subdomain using a disposable store
and invalid token returned HTTP 401 API key missing or invalid. No actual
workspace subdomain or authenticated account was tested. No writes were made.

## Hyperise research status

The published API links point to support.hyperise.com/en/api/Creating-API-token
and /en/api/Image-Views-API. Browser retrieval timed out; a direct HTTPS request
failed certificate validation with a self-signed-certificate error. The route
remains pending because its API host and authentication have not been verified
from accessible primary documentation. This is not evidence that the service
itself has stopped working.

## GiantCampaign validation

The [provider reference](https://giantcampaign.com/developers/) requires api_token
in the query for both reads and POST requests. The new trusted catalogue query
authentication style inserts the stored token with URL encoding, refuses
caller-supplied token overrides, and omits authenticated URLs from connection
errors. Bound GET/POST tests include reserved characters in the credential;
additional tests cover overrides, foreign paths and error output. Redirects
remain disabled. JSON request-body acceptance is not claimed; documented
non-secret parameters can be supplied in the path query.

The two positive cases failed before implementation. All nine new cases now
pass; the focused suite passes 278 tests and Ruff passes. A live bound GET /lists
with an invalid token returned HTTP 401 Unauthenticated. No campaign or
subscriber was changed; authenticated account verification remains pending.

## EmailListVerify and LeadDyno validation

Four new request cases failed before their routes were added and pass afterward.
The focused suite passes 282 tests; Ruff passes. EmailListVerify's
[live OpenAPI](https://api.emaillistverify.com/api-doc-json) confirms the production
host, x-api-key, credit read and JSON email-job body. LeadDyno's official tracking
guide documents key-header authentication and form-encoded visitor creation.
Tests verify both GET and POST encoding without creating remote records.

Bound reads with disposable stores and deliberately invalid keys returned
HTTP 401 INVALID_API_KEY for EmailListVerify /credits and HTTP 401 Unauthorized
for LeadDyno /visitors. No verification credits were spent and no affiliate
tracking data was created. Authenticated account acceptance remains pending.

## Acumbamail validation

The [API reference](https://acumbamail.com/apidoc/) specifies function-based
paths under /api/1/, GET parameters and POST form data. A trusted catalogue flag
places the stored auth_token in the form for POST instead of the URL. Tests
cover both methods, encoded credentials, nested fields, preserving the caller's
body and refusing a credential override. The two positive cases failed before
implementation; all three new cases pass. The focused suite passes 285 tests;
Ruff passes.

Bound GET and POST calls to /getLists/ with a disposable credential store and
an invalid token both returned HTTP 401 Unauthorized. These are rejection checks,
not authenticated list access. No live subscriber or campaign mutations ran.

## EmailVerify.io validation

Added the documented mixed GET-query and POST-JSON key authentication, preserving
caller bodies and rejecting credential overrides. Both new tests failed before
implementation and pass afterward. The focused suite passes 287 tests; Ruff passes.
A bound GET /v2/check-account-balance using disposable invalid credentials returned
HTTP 401 with Key not found. This establishes reachability, not authenticated
account acceptance. No verification jobs were submitted.
