# REST connector implementation progress

Scope: implement the 144 catalogue connectors identified with missing REST hosts.
This is an implementation ledger, not a claim of live-account verification.

One hundred and nine routes now have offline request-contract coverage through the real connector
registry and credential store. Tests assert the outbound origin, version prefix,
credential header, query and JSON body. Credentials are never followed through
HTTP redirects. Production authenticated reads and writes remain unverified for these
one hundred and nine routes. The remaining 35 connectors still need provider research and code.

## Implemented routes

Paths passed to tools are relative to the configured base. API credentials belong
in the connector secret store, never tool arguments or configuration fields.
ActiveCampaign additionally needs `api_domain` from its account settings.
GetResponse currently covers SMB accounts; MAX routing remains to be implemented.
CloudConvert currently uses its production automatic-region API, not its sandbox.

| Connector | Provider reference | Setup |
|---|---|---|
| `everwebinar` | [API reference](https://support.webinarjam.com/en/collections/19655442-everwebinar-api) | Store the approved account API key. All operations use POST. /webinars takes an empty body; /webinar takes webinar_id and optional timezone. Dotobot adds api_key in the form body. Use API-returned schedule IDs for registration. |
| `gosquared` | [Authentication](https://www.gosquared.com/docs/configuration/) | Store the API Access key; Dotobot adds api_key to the query. Supply site_token for project endpoints, in GET query or POST path query. Include the API/version prefix. GET /now/v3/overview; POST /tracking/v1/event with JSON event. Key scopes apply. Use a test project for tracking. |
| `google_analytics` | [Data API reference](https://developers.google.com/analytics/devguides/reporting/data/v1/rest) | Store a current OAuth access token with analytics.readonly or analytics scope and property access. Enable the Data API. Include /v1beta or /v1alpha in paths. GET /v1beta/properties/ID/metadata; POST /v1beta/properties/ID:runReport with dimensions, metrics and dateRanges. Use limit and offset to constrain responses. Token creation/refresh, Admin API and Measurement Protocol are not implemented by this route. |
| `enginemailer` | [Campaign API reference](https://enginemailer.zendesk.com/hc/en-us/articles/360003129972-Campaign-REST-API-GETTING-STARTED) | Store the profile API key, sent in APIKey. Campaign API requires a paid plan. Paths omit /restapi. GET /campaign/emcampaign/GetCategoryList; POST /Campaign/EMCampaign/CreateCampaign with JSON. Check Result.Status and Result.StatusCode even when HTTP is 200. |
| `adrapid` | [Provider documentation](https://user-api-docs.adrapid.com/) | Store the account API token as Bearer. The linked OpenAPI server uses /v1/api. GET /me reads account data; POST /banners takes JSON templateId and modes. Poll /banners/ID until ready and inspect files. Generation and completed export are separate; binary downloads are unsupported by the JSON tool. |
| `contentdrips` | [Provider documentation](https://developer.contentdrips.com/) | Store the API Management key. Uses Bearer and JSON Content-Type. POST /render with template_id, output and content_update queues generation; poll /job/JOB_ID/status then /job/JOB_ID/result. HTTP 202 means queued. Carousel uses /render?tool=carousel-maker. This route targets generation, not the separate Embed SDK API. |
| `callpage` | [Provider documentation](https://callpage.github.io/documentation-rest/) | Store the dashboard API key, sent directly in Authorization. GET /v3/external/calls/history reads history. PATCH /v1/external/calls/CALL_ID/fields/FIELD_ID takes JSON value. Include the documented version in paths; inspect hasError and data. Token/widget scopes apply. Calling and messaging may contact people. |
| `brandmentions` | [Provider documentation](https://help.brandmentions.com/en/articles/12814618-how-do-i-authenticate-api-requests-safely) | Store the provider-issued API key. GET /command.php with query command=GetRemainingCredits reads credits; ListProjects lists projects. Dotobot supplies api_key. Commands can also mutate data or spend credits, even through GET. Provider-enabled API access is required. |
| `discourse` | [Provider documentation](https://docs.discourse.org/) | Store an admin-generated API key. Configure api_domain as a hostname and api_username. Uses Api-Key and Api-Username. GET /categories.json reads categories; POST /posts.json takes JSON title and raw. Key scopes/user permissions apply. Root-host HTTPS and ASCII usernames are supported; subdirectory installations and User API key authorization are not implemented. |
| `jvzoo` | [Provider documentation](https://api.jvzoo.com/docs/) | Store the API Application key alone; Basic auth uses it as username and x as password. Include /v3.0, /v2.1 or /v2.0 in paths. GET /v3.0/transactions takes start_date and end_date. JSON writes are supported; inspect meta.status and results. |
| `hypeauditor` | [Provider documentation](https://hypeauditor.com/swagger/public-api/v1/) | Store the API token and configure numeric client_id. Uses X-Auth-Hash and X-Auth-Id. GET /api/v1/media-plan/plans lists plans; POST with JSON title creates a plan. Include full API prefixes in paths. API entitlement and credits apply; report requests may consume credits. |
| `funnelcockpit` | [Provider documentation](https://api.funnelcockpit.com/) | Store the private API key, sent directly in Authorization without Bearer. GET /me verifies the user; GET /email/tags uses zero-based page and limit. POST /email/tag takes JSON contactId and tagId. Subscriber/tag operations may trigger automations; plan access applies. |
| `kickofflabs` | [Provider documentation](https://support.kickofflabs.com/developer/common-api-behavior/) | Store the campaign API Access key. Uses Bearer with JSON Content-Type. GET /campaigns lists campaigns; POST /CAMPAIGN_ID/ creates or updates a lead with email or phone_number. Lead changes may trigger campaign automations. |
| `flippingbook` | [Provider documentation](https://apidocs.flippingbook.com/) | Store an Online API key; uses Bearer. GET /fbonline/publication lists publications with count and offset. POST the same path with JSON name and url for a reachable PDF. Check success and source conversion status. Plan access applies. This route targets FlippingBook Online, not desktop Publisher. |
| `lagrowthmachine` | [Provider documentation](https://documenter.getpostman.com/view/32966764/2sBXqFM2Vv) | Store the Settings > API key; uses Bearer. GET /members tests access. Paths are relative to /flow. Bodies are JSON except /audiences, /leads/status and /campaigns/ID/settings or status, which use form encoding. POST /audiences/create takes name. Campaign and inbox actions can trigger outreach. |
| `docupost` | [Provider documentation](https://help.docupost.com/developer-documentation/send-letter-api) | Store the Developer API token. POST /sendletter or /sendpostcard uses URL-encoded non-secret query parameters in path; Dotobot adds api_token securely. Supply sender/recipient and PDF or image URLs as documented. Enable account Sandbox Mode for testing. These operations send physical mail and can incur charges; no read endpoint is documented here. |
| `curated` | [Provider documentation](https://support.curated.co/help/getting-started-with-the-api) | Store the Account API Key. Dotobot quotes it in Authorization: Token token. GET /publications retrieves IDs. POST /publications/ID/issues/ creates a draft. Bodies use JSON. Publishing requires the website. |
| `dribbble` | [Provider documentation](https://developer.dribbble.com/v2/) | Store an OAuth access token, used as Bearer. GET /user or /user/shots reads account data. PUT /shots/ID updates metadata with upload scope. The documented JSON body uses application/x-www-form-urlencoded Content-Type. OAuth lifecycle, multipart uploads and binary responses are unsupported here. |
| `apexverify` | [Provider documentation](https://documentation.apexverify.com/api-reference/api-authentication) | Store the API key, sent in X-Api-Key. GET /account/credits reads the balance. POST /unit accepts JSON type (email or phone), target_country and unit. Verification consumes credits; review use_global_cache before submitting data. Multipart uploads and binary exports are unsupported by the generic JSON tool. |
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
| `acymailing` | Implemented customer-host REST route; request contracts pass; real installation and license key needed for live acceptance |
| `add_to_calendar_pro` | Implemented REST route; event read/create contracts pass; live invalid key rejected; authenticated acceptance open |
| `adhook` | Pending: official OpenAPI found; server prefix and credential format are omitted; live invalid-credential responses are inconclusive |
| `adrapid` | Implemented; request contracts pass; invalid token rejected live, account acceptance pending |
| `adroll` | Implemented PAT route with scalar multipart writes; tests pass; invalid application key rejected live; account acceptance and binary uploads pending |
| `adtraction` | Implemented; request contracts pass; authenticated account verification pending |
| `aimtell` | Implemented REST route; website read/update contracts pass; live invalid API key rejected; authenticated acceptance open |
| `airship` | Implemented regional HTTP/OAuth routes; version/auth contracts pass; invalid tokens rejected live; account acceptance pending |
| `apexverify` | Implemented; request contracts pass; invalid key rejected live, authenticated account acceptance pending |
| `appsflyer` | Implemented hq1 API V2 token route; contracts pass; invalid token rejected live; account acceptance pending |
| `arpoone` | Implemented v1.2 REST route; balance read and short-link JSON contracts pass; live invalid key rejected; authenticated acceptance open |
| `asters` | Implemented documented REST route; request contracts pass; live server names a different auth header; authenticated acceptance unresolved |
| `attentive` | Route and offline request tests added; live verification pending |
| `autoklose` | Implemented REST route; request contracts pass; live invalid key explicitly rejected; authenticated acceptance open |
| `automizy` | Pending provider research and implementation |
| `beamer` | Implemented; request-contract tests pass; production account verification pending |
| `benchmark_email` | Implemented; request contracts pass; authenticated account verification pending |
| `bigmailer` | Route and offline request tests added; live verification pending |
| `botconversa` | Implemented; request contracts pass; authenticated account verification pending |
| `brandmentions` | Implemented; request contracts pass; live Python request blocked by certificate-chain validation |
| `builderall_mailingboss` | Implemented documented route; request contracts pass; invalid-key live read returns 404; authenticated acceptance open |
| `buysellads` | Implemented Advertiser API; four reporting contracts pass; live invalid and example keys rejected; authenticated acceptance open |
| `callpage` | Implemented; request contracts pass; invalid key rejected live, account acceptance pending |
| `callrail` | Route and offline request tests added; live verification pending |
| `campaign_cleaner` | Implemented; request contracts pass; authenticated account verification pending |
| `campaign_monitor` | Route and offline request tests added; live verification pending |
| `campaignhq` | Pending provider research and implementation |
| `campayn` | Implemented; request contracts pass; authenticated account verification pending |
| `cardly` | Route and offline request tests added; live verification pending |
| `catch_all_verifier` | Implemented REST route; credits GET and verification JSON contracts pass; live invalid key rejected; authenticated acceptance open |
| `chatrace` | Implemented bot-account REST route; mixed form/JSON contracts pass; live invalid key rejected; authenticated acceptance open |
| `cleverreach` | Implemented; request-contract tests pass; production account verification pending |
| `clevertap` | Implemented regional host and header pair route; six-host request coverage passes; invalid-credential live read returns generic 400; authenticated acceptance open |
| `clickfunnels` | Implemented; request contracts pass; authenticated account verification pending |
| `cloud_convert` | Route and offline request tests added; live verification pending |
| `cometly` | Implemented; request contracts pass; authenticated account verification pending |
| `constant_contact` | Implemented; request-contract tests pass; production account verification pending |
| `contentdrips` | Implemented; request contracts pass; queue GET returns 200 with invalid token, authenticated acceptance pending |
| `convertkit` | Implemented; request-contract tests pass; production account verification pending |
| `copicake` | Implemented; request-contract tests pass; production account verification pending |
| `coupontools` | Implemented modern v4 route with secret header pair; request contracts pass; invalid pair rejected live; legacy API remains unsupported |
| `crowdpower` | Implemented; request contracts pass; authenticated account verification pending |
| `curated` | Implemented; request contracts pass; invalid-key probe returns 404 Record not found, authenticated acceptance pending |
| `cyberimpact` | Implemented; request-contract tests pass; production account verification pending |
| `demandbase` | Implemented current JWT route; request contracts pass; invalid token rejected live; account acceptance pending |
| `demio` | Pending: public Apiary reference did not expose the contract; blueprint endpoint requires authentication; host and auth contract still need verification |
| `discourse` | Implemented configurable-host route; request contracts pass; live forum acceptance pending |
| `docupost` | Implemented; request contracts pass; live missing-data rejection, authenticated acceptance pending |
| `doppler` | Route and offline request tests added; live verification pending |
| `dribbble` | Implemented stored-token route; request contracts pass; authenticated account testing pending |
| `drip` | Route and offline request tests added; live verification pending |
| `dripcel` | Implemented MarTech REST route; request contracts pass; live invalid key rejected; authenticated acceptance open |
| `dropcontact` | Route and offline request tests added; live verification pending |
| `dux_soup` | Implemented HMAC REST route; URL/body signing and envelope tests pass; live invalid token rejected; authenticated acceptance open |
| `dynamic_content_snippet` | Pending provider research and implementation |
| `dynapictures` | Route and offline request tests added; live verification pending |
| `egoi` | Route and offline request tests added; live verification pending |
| `easypromos` | Implemented; request contracts pass; authenticated account verification pending |
| `easysendy` | Implemented JSON REST route; read/write contracts pass; invalid-key live response is empty OK and does not prove authentication |
| `echtpost_postcards` | Pending provider research and implementation |
| `ecologi` | Implemented; request contracts pass; authenticated account verification pending |
| `email_on_acid` | Route and offline tests added; public sandbox authentication and read passed; production verification pending |
| `emailable` | Route and offline request tests added; live verification pending |
| `emailchef` | Implemented authkey REST route; list read/create contracts pass; live invalid token rejected; authenticated acceptance open |
| `emaillistverify` | Implemented; request contracts pass; authenticated account verification pending |
| `emailoctopus` | Implemented; request-contract tests pass; production account verification pending |
| `emailverify_io` | Implemented; request contracts pass; invalid key rejected live, authenticated account acceptance pending |
| `emelia` | Pending provider research and implementation |
| `encharge` | Implemented own-account REST route; request contracts pass; live invalid key rejected; authenticated acceptance open |
| `endorsal` | Pending: official property-key Bearer authentication verified; endpoint reference and REST host still unresolved |
| `engage` | Pending provider research and implementation |
| `enginemailer` | Implemented; request contracts pass; live invalid key rejected in HTTP 200 response body; authenticated acceptance pending |
| `enormail` | Implemented; request contracts pass; authenticated account verification pending |
| `esputnik` | Implemented; request-contract tests pass; production account verification pending |
| `eventbrite` | Implemented; request-contract tests pass; production account verification pending |
| `everwebinar` | Implemented; form POST read contracts pass; invalid key rejected live; account acceptance pending |
| `exact_mails` | Pending: current website gates API documentation behind account creation; backend differs from older integration examples |
| `facebook` | Pending provider research and implementation |
| `feedblitz` | Implemented XML REST route; transport contracts pass; invalid key rejected in HTTP 200 XML; account acceptance pending |
| `flexmail` | Pending provider research and implementation |
| `flippingbook` | Implemented Online API; request contracts pass; invalid key rejected live, account acceptance pending |
| `fomo` | Route and offline request tests added; live verification pending |
| `freshmarketer` | Implemented standalone account-subdomain route; request contracts pass; real host/key needed for live acceptance |
| `funnelcockpit` | Implemented; request contracts pass; invalid-key request rejected live, account acceptance pending |
| `getemails` | Pending provider research and implementation |
| `getresponse` | Route and offline request tests added; live verification pending |
| `getswift` | Pending provider research and implementation |
| `giantcampaign` | Implemented; request contracts pass; authenticated account verification pending |
| `gist` | Implemented; request contracts pass; authenticated account verification pending |
| `gitter` | Pending provider research and implementation |
| `gobio_link` | Pending provider research and implementation |
| `goodbits` | Pending: website, API and support hosts fail DNS resolution; official API contract unavailable |
| `google_ad_manager` | Pending provider research and implementation |
| `google_ads` | Pending provider research and implementation |
| `google_analytics` | Implemented; Data API request contracts pass; invalid token rejected live; authenticated account acceptance pending |
| `google_calendar` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `google_drive` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `google_sheets` | Implemented stored-token route; request contracts pass; OAuth lifecycle and live account verification pending |
| `gosquared` | Implemented; request contracts pass; official public demo read passes; production account acceptance pending |
| `gozen_growth` | Pending provider research and implementation |
| `grade_us` | Pending provider research and implementation |
| `greenspark` | Implemented; request contracts pass; authenticated account verification pending |
| `growsurf` | Route and offline request tests added; live verification pending |
| `herobot` | Pending provider research and implementation |
| `heysummit` | Pending: official REST v2 help found, but linked api-v2 and api-docs documentation hosts return 403; full REST contract unverified |
| `hippo_video` | Implemented stored authentication-token route; contracts pass; invalid token/email rejected live; account acceptance pending |
| `humanitix` | Implemented; request contracts pass; authenticated account verification pending |
| `hypeauditor` | Implemented; request contracts pass; invalid credentials rejected live, account acceptance pending |
| `hyperise` | Pending: official API support pages currently fail TLS certificate validation; host and authentication still require verification |
| `icontact` | Implemented standard API; three-header and JSON-array contracts pass; live invalid username rejected; authenticated acceptance open |
| `impression` | Pending provider research and implementation |
| `indiefunnels` | Pending provider research and implementation |
| `infusionsoft` | Implemented; request-contract tests pass; production account verification pending |
| `inksprout` | Pending provider research and implementation |
| `instabot` | Implemented master-key REST route; read/query contracts pass; live API key rejected; authenticated acceptance open |
| `instagram` | Pending provider research and implementation |
| `instasent` | Route and offline request tests added; live verification pending |
| `jellyreach` | Pending provider research and implementation |
| `joggai` | Implemented; request-contract tests pass; production account verification pending |
| `jvzoo` | Implemented; request contract passes; invalid key rejected live, account acceptance pending |
| `kartra` | Implemented form POST API; nested read contract passes; invalid app ID explicitly rejected; authenticated acceptance open |
| `kickofflabs` | Implemented; request contracts pass; invalid key rejected live, account acceptance pending |
| `kingsumo` | Pending provider research and implementation |
| `klenty` | Implemented; request contracts pass; authenticated account verification pending |
| `kyvio` | Pending provider research and implementation |
| `lagrowthmachine` | Implemented; request contracts pass; invalid key rejected live, account acceptance pending |
| `lahar` | Implemented Ramper Marketing conversion route; JSON contract passes; incomplete live request rejected; authenticated acceptance open |
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

## ApexVerify validation

The official OpenAPI specification confirms the production host, X-Api-Key header
and JSON unit verification schema. Both new contract tests failed before the route
was added and pass afterward. The focused suite passes 289 tests; Ruff passes.
A disposable bound GET /account/credits rejected an invalid key with HTTP 401
Unauthorized. No verification was submitted; authenticated acceptance remains open.

## Dribbble validation

The official v2 overview specifies the host, bearer token and unusual JSON body
with form Content-Type; the shots reference confirms PUT and upload scope.
Two new tests failed before implementation and pass afterward. The focused suite
passes 291 tests; Ruff passes. A disposable bound GET /user rejected an invalid
token with HTTP 401 Bad credentials. No account writes or uploads were attempted;
authenticated acceptance and provider acceptance of write encoding remain open.

## Curated validation

The official documentation confirms the v3 host, quoted-token header, JSON bodies
and bodyless draft-creation endpoint. The route test failed before implementation;
both new tests now pass, including escaping quotes and backslashes in a stored key.
The focused suite passes 293 tests; Ruff passes. A disposable bound GET /publications
with an invalid key returned HTTP 404 Record not found. This proves an HTTP response
from the documented endpoint but does not prove authentication handling or account
access. No drafts were created on the live service.

## DocuPost validation

Two request tests cover letter and postcard endpoint paths, POST, query parameters
and encoded stored credentials without a bearer header. Both failed before the route
was added. The focused suite passes 295 tests; Ruff passes. A live bound POST
/sendletter with an invalid token and no mailing data returned HTTP 400 MISSING_DATA
for to_name. This proves endpoint reachability and input validation, not token
acceptance or mailing success. No mailing job was submitted. Sandbox Mode is a
provider account setting, not a request parameter invented by Dotobot.

## La Growth Machine validation

The vendor integrations page links the Postman reference. Its collection confirms
Bearer authentication at apiv2.lagrowthmachine.com/flow and mixed JSON/form bodies.
Seven new tests failed before implementation and pass afterward. The focused suite
passes 302 tests; Ruff passes. A disposable bound GET /members returned HTTP 401
Invalid apikey. No campaign, lead or inbox writes were attempted; authenticated
account acceptance remains open.

## FlippingBook validation

The official API reference confirms the gateway, Bearer API key, publication list
and JSON publication creation with a PDF URL. Two new tests failed before the route
was added and pass afterward. The focused suite passes 304 tests; Ruff passes.
A disposable bound publication GET rejected an invalid key with HTTP 403 InvalidApiKey.
No publication was created. Authenticated access and PDF conversion remain unverified.

## KickoffLabs validation

The current official reference documents v2, Bearer authentication for JSON requests
and lead creation/update. Two new tests failed before implementation and pass after.
The focused suite passes 306 tests; Ruff passes. A disposable bound GET /campaigns
rejected an invalid key with HTTP 409 Invalid API Access. No leads were submitted;
authenticated account acceptance remains open.

## FunnelCockpit validation

The official OpenAPI document confirms the root host, raw Authorization API key,
current-user GET and JSON tag assignment. Two tests failed before implementation
and pass afterward. The focused suite passes 308 tests; Ruff passes. A disposable
bound GET /me with an invalid key returned HTTP 401 Unauthorized access. This is
reachability/rejection evidence, not authenticated acceptance. No tags were assigned.

## HypeAuditor validation

The official public API OpenAPI document specifies both authentication headers and
media-plan read/write contracts. Numeric client_id validation rejects missing,
non-numeric and injected header values before transport. Four initial tests failed
before implementation; all five new tests pass. The focused suite passes 313 tests;
Ruff passes. A disposable bound media-plan GET using client ID 0 and an invalid token
returned HTTP 403 Access denied. No plans or reports were created; authenticated
account acceptance remains open.

## JVZoo validation

The official combined OpenAPI reference confirms all supported version prefixes
and Basic authentication with a literal x password. The new test failed before
implementation and passes afterward. The focused suite passes 314 tests; Ruff passes.
A disposable bound v3 transaction GET returned HTTP 401 Invalid API Key present.
No account mutations were attempted; authenticated acceptance remains open.

## Midpoint completeness and full-suite check

At commit 4e999d9, the catalogue contains 72 implemented routes and 72 pending
routes from the original 144. The full status table was missing ApexVerify and
EmailVerify.io rows even though their implementation sections existed; both rows
are restored. An exact set comparison confirms all 144 unique entries match the
catalogue and their implemented/pending classifications agree with route metadata.

Full pytest run with a short temporary directory: 3450 passed, 2 skipped, 2 failed
(3454 collected). The failures are test_listener_user_data_dirs_reads_listening_cmdline
and test_run_session_raises_when_display_stays_bound. Their assertion failures and
missing /workspace diagnostic match the earlier unchanged c33bb96 baseline run.
No additional failures appeared. This is code-level validation, not authenticated
provider acceptance. No runtime source changed during this checkpoint.

## Discourse validation

Eight cases cover configured hosts, username validation, authentication headers and
JSON topic creation. The valid read/write tests failed before implementation.
The initial implementation exposed shared template handling that wrongly validated
api_username as a hostname; only fields used by the trusted URL template are now
subject to hostname validation. Username validation remains separate and fail-closed.
The focused suite passes 322 tests; Ruff passes. No real forum or token was used;
no topics were posted. Live authenticated acceptance remains pending.

## BrandMentions validation

Two documented command-query tests failed before implementation and pass afterward.
The focused suite passes 324 tests; Ruff passes. The bound invalid-key balance probe
returned a connection error. A credential-free Python probe identifies certificate
verification failure: unable to get local issuer certificate. The system curl client
can reach the same HTTPS endpoint (400 for missing parameters), so this is a runtime
trust-chain difference, not evidence the provider is down. TLS verification remains
enabled. Authenticated acceptance and Python-runtime connectivity remain unresolved.

## CallPage validation

The provider reference mixes v3 call-history and v1 update endpoints. The route
preserves those prefixes and uses the documented raw Authorization key. Two tests
failed before implementation and pass afterward. The focused suite passes 326 tests;
Ruff passes. A disposable bound v3 history GET returned HTTP 401 access-denied.
No calls, messages or field updates were submitted; authenticated acceptance remains open.

## ContentDrips validation

Two request tests failed before implementation and pass afterward. The focused suite
passes 328 tests; Ruff passes. A disposable bound GET /queue/stats returned HTTP 200
with an invalid token. A nonexistent-job status GET returned HTTP 404 Job not found.
These establish reachability but do not verify authentication: the observed reads
do not reject invalid credentials despite the documentation saying all endpoints
require Bearer authentication. No render was submitted; authenticated generation
and result retrieval remain unverified.

## AdRapid validation

The official overview links the User API specification, which declares /v1/api as
its base prefix. This takes precedence over abbreviated overview examples omitting
that prefix. Two new tests failed before implementation and pass afterward. The
focused suite passes 330 tests; Ruff passes. A disposable bound GET /me returned
HTTP 401 JWT web token malformed. No banners were generated; authenticated account
access and export completion remain unverified.

## Enginemailer campaign API

Added the documented /restapi host and APIKey header. [Category lookup](https://enginemailer.zendesk.com/hc/en-us/articles/360003226071-Get-Category-List) and [JSON campaign creation](https://enginemailer.zendesk.com/hc/en-us/articles/360003152791-Create-Campaign) are covered through the bound connector. The campaign API requires a paid plan and a verified sender domain for creation. Check Result.Status and Result.StatusCode rather than HTTP status alone.

A disposable-store GET category lookup with an invalid key returned HTTP 200 with Result.StatusCode 500 and API Key Not Found. This proves reachability and application-level rejection, not authenticated account acceptance. No campaign was created or sent. All 332 focused tests pass; Ruff passes. There are now 78 implemented routes and 66 pending. The midpoint full-suite result predates this addition.

## Google Analytics Data API

The official [Data API reference](https://developers.google.com/analytics/devguides/reporting/data/v1/rest) and [runReport method](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport) establish the host, OAuth scopes and report JSON shape. Bound request tests cover metadata GET and colon-suffixed report POST. A disposable-store metadata request with an invalid token returns HTTP 401 UNAUTHENTICATED. No real property data was accessed.

All 334 focused tests and Ruff pass. The ledger contains 79 implemented and 65 pending routes. The midpoint full-suite result predates this addition.

## GoSquared

Added query-key authentication at api.gosquared.com, preserving API/version paths and project site_token. Request tests cover the documented [overview read](https://www.gosquared.com/docs/now/overview/) and [JSON event tracking](https://www.gosquared.com/docs/tracking/event/).

The bound overview call with the documented public demo key and site token returned HTTP 200 with visitor, page and summary metrics. The same read with an invalid key returned HTTP 401, API key not authorised. No tracking request was sent. Production acceptance remains open. All 336 focused tests and Ruff pass; 80 routes are implemented and 64 remain pending. The midpoint full suite predates this addition.

## EverWebinar

The current official API uses api.webinarjam.com/everwebinar. [Webinar listing](https://support.webinarjam.com/en/articles/15370154-retrieve-a-full-list-of-all-webinars-published-in-your-account-everwebinar-api) and [webinar details](https://support.webinarjam.com/en/articles/15370155-get-details-about-one-particular-webinar-from-your-account-everwebinar-api) both require form POST. Tests cover key escaping, body preservation, paths and timezone encoding.

A bound disposable-store POST /webinars with an invalid key returned HTTP 401 with a valid-key-required error. No registration or subscription change was attempted. All 338 focused tests and Ruff pass. There are 81 implemented routes and 63 pending; the midpoint full suite predates this addition.

## Airship HTTP API

The [official introduction](https://www.airship.com/docs/developer/rest-api/ua/introduction/) documents dashboard Bearer tokens on go.urbanairship.com and the mandatory application/vnd.urbanairship+json; version=3 Accept header. Catalogue-owned Accept metadata now supplies this version without allowing caller header overrides. The [OpenAPI specification](https://www.airship.com/docs/openapi/go/spec.json) confirms GET /api/channels.

The bound North American channels probe returned HTTP 401 with Unauthorized and error_code 40101. No message was sent. The initial route covered North American dashboard tokens; the extension below adds other regions and authentication modes. CSV uploads remain unsupported. Production account acceptance remains open. All 339 focused tests and Ruff pass; 82 routes are implemented and 62 pending. The midpoint full suite predates this addition.

## Airship regional and authentication extension

Configure region as us or eu and auth_mode as bearer, basic or oauth; omitted values preserve us/bearer. Dashboard and Basic credentials use the regional HTTP host; OAuth access tokens use the documented regional OAuth host. Basic secrets contain appKey:appSecret or appKey:masterSecret. Token issuance/refresh is not implemented. Only the four official hosts can be selected; unknown modes or regions block transport.

Nine additional tests cover six region/auth combinations and invalid configuration. Bound probes against the EU HTTP host and both OAuth hosts returned HTTP 401 for invalid tokens. All 348 focused tests and Ruff pass. The overall route count remains 82 with 62 pending. No audience was contacted.

## AdRoll implementation research

The [official getting-started guide](https://apidocs.nextroll.com/guides/get-started.html) confirms https://services.adroll.com. Personal access tokens use Authorization: Token TOKEN, together with the application client ID in the apikey URL query parameter on every method. The application ID must not move into POST/PUT/PATCH bodies. The documented first read is GET /api/v1/organization/get_advertisables.

The [CRUD examples](https://apidocs.nextroll.com/crud-api/examples.html) use multipart form fields for writes, including POST /api/v1/advertisable/create; image creation includes file upload. This differs from the current generic JSON/urlencoded modes. Before marking this route implemented, add the supported request encoding and tests for the independent client-ID query, token header and body format. OAuth is also documented separately. No AdRoll API call or account mutation was made in this research pass; the route remains pending.

## AdRoll route implementation

Added the documented host, Token authentication and scalar multipart form bodies. Supply the application client ID in apikey URL query; it remains separate from the stored personal token. Tests parse MIME parts to verify field values and reject header-injection field names and nested file objects before transport. File uploads and OAuth consent are not supported.

A bound GET /api/v1/organization/get_advertisables with invalid personal token and client ID returned HTTP 401 apiproxy:2 (invalid API key). This verifies reachability and application-key rejection, not personal-token or account acceptance. No advertising mutation was attempted. All 351 focused tests and Ruff pass. There are 83 routes and 61 pending. The midpoint full suite predates this addition.

## Demandbase

Added uapi.demandbase.com using current JWT Bearer access tokens and JSON. [API key-set guidance](https://support.demandbase.com/hc/en-us/articles/38999526296603-Generate-and-Manage-API-Key-Sets) explains token generation and the eight-hour lifetime. The connector accepts a stored access token; issuance and refresh are not implemented. [Export-job listing](https://developer.demandbase.com/reference/fetchexportjobsinfo) and [Intent query](https://developer.demandbase.com/reference/companyintent-1) have bound request-contract coverage. Product permissions apply and Intent is beta.

A disposable-store GET /reporting/v1/usage?apiProduct=b2bapi returned HTTP 401 Authentication Failed - Unauthorized with an invalid token. No export or paid data request was submitted. All 353 focused tests and Ruff pass. There are 84 routes and 60 pending. The midpoint full-suite result predates this addition.

## Hippo Video

The [library API](https://help.hippovideo.io/support/solutions/articles/19000095981-video-library-api) and [personalization API](https://help.hippovideo.io/support/solutions/articles/19000095986-generate-personalized-videos-through-api) use authentication_token and user email. The connector stores the generated token, adds it to GET query or write JSON, and preserves the caller body. Library and detail reads plus personalization JSON have offline request coverage. [Token generation](https://help.hippovideo.io/support/solutions/articles/19000095978-api-authorization) revokes an existing token and is not performed automatically.

The bound library GET with an invalid token returned HTTP 403 OAuthException, explicitly reporting a wrong token or email mismatch. No token was generated and no video changed. File/import workflows remain unverified. All 356 focused tests and Ruff pass; 85 routes are present and 59 pending. The midpoint full suite predates this addition.

## FeedBlitz XML REST API

The [official access guide](https://developer.feedblitz.com/docs/rest-api/accessing-the-api/) specifies app.feedblitz.com/f.api, query key authentication, required User-Agent and XML write payloads. Added XML encoding through body={"xml": "complete XML document"}; the adapter sends the string as UTF-8 XML. Tests verify transport and escaping with synthetic XML, not accepted business writes. Wrong wrapper shapes fail before transport. Simple and Transactional APIs remain separate.

The bound GET /user with an invalid key returned HTTP 200 containing rsp stat=fail and Invalid API key. Inspect XML response status rather than HTTP alone. No subscription or account write was attempted. All 362 focused tests and Ruff pass; 86 routes are present and 58 pending. The midpoint full suite predates this addition.

## Endorsal authentication research

The [official developer center](https://developers.endorsal.io/) confirms property-specific keys generated in Account > API and Authorization: Bearer authentication. GET may alternatively use key in the query. The linked /docs/endorsal/ endpoint reference returned the same developer landing content, without an API host or endpoint specification. Catalogue guidance now points to this verified official source. Host, version and request contracts remain pending; no route or live-account success is claimed.

## AppsFlyer hq1 API

The [app-list reference](https://dev.appsflyer.com/hc/reference/app-list-ad-nets-api-get) specifies hq1.appsflyer.com and API V2 Bearer authentication. Added bound contracts for the app list and [click-signing test](https://dev.appsflyer.com/hc/reference/click-signing-test-post) JSON request. Paths retain the service/version prefix. App lists require pagination; access depends on account permissions. Other service hosts and binary export workflows remain outside this route.

A disposable-store GET /api/mng/apps?limit=1 with an invalid token returned HTTP 401 Authentication error. No signing test or account mutation was submitted. All 364 focused tests and Ruff pass. There are 87 routes and 57 pending; the midpoint full suite predates this addition.

## Freshmarketer standalone API

The [official API reference](https://developer.freshmarketer.com/) documents https://SUBDOMAIN.freshmarketer.com/mas/api/v1 and the fm-token header. Added account subdomain configuration, contacts transport coverage and JSON subscription-type creation coverage. Five invalid host cases block transport. This route covers the documented standalone product, not separate Freshworks CRM Suite hosts.

No real account subdomain/key is available for a live acceptance check; no contact or subscription data was changed. All 371 focused tests and Ruff pass. There are 88 routes and 56 pending; the midpoint full suite predates this addition.


## Encharge REST route

The [official developer documentation](https://docs.encharge.io/api-documentation) links the [current ReDoc specification](https://app-encharge-resources.s3.amazonaws.com/merged.yaml), which specifies https://api.encharge.io/v1 and X-Encharge-Token for own-account API keys. Added GET /people/all and JSON POST /tags request coverage. The older raw definition linked from the documentation uses a different S3 bucket; the current ReDoc specification explicitly documents header-key authentication.

A disposable connector-store probe of GET /people/all?limit=1 returned HTTP 401, errorCode 10082, rejecting the invalid token. No customer records were read or written. Authenticated account acceptance remains open. Partner OAuth setup, automatic token refresh and top-level array bodies for bulk people creation are not implemented. The separate Ingest API is outside this REST host. Focused suite: 373 passed; Ruff clean.


## Aimtell REST route

The [official reference](https://developers.aimtell.com/api-reference/introduction) and [website OpenAPI specification](https://developers.aimtell.com/api-reference/sites-openapi.json) document the /prod base and X-Authorization-Api-Key header. Added website-list GET and JSON website-update PUT contracts. GET /sites/ supports limit/skip pagination; PUT /site/ID takes name and optional icon.

A disposable connector-store request to GET /sites/?limit=1 returned HTTP 403 with Invalid API Key. No accepted write or push notification was attempted. Real-account acceptance remains open. Focused suite: 375 passed; Ruff clean.


## Add to Calendar PRO REST route

The [official API overview](https://docs.add-to-calendar-pro.com/api/introduction), [authentication guide](https://docs.add-to-calendar-pro.com/api/auth), and [event reference](https://docs.add-to-calendar-pro.com/api/events) establish the v1 host, raw Authorization key, and JSON event format. Added GET /event/all and nested dates POST /event contracts. Organization keys have scopes and optional expiry. Event creation publishes immediately and some updates consume credits; no live write was attempted. Separate ICS download hosts are not part of this JSON route.

A disposable connector-store GET /event/all?page=1 returned HTTP 401, Not authenticated. Authenticated acceptance remains open. Focused suite: 377 passed; Ruff clean.


## Catch-all Verifier REST route

The [official reference](https://catchallverifier.readme.io/reference/post_api-v1-verify-single) embeds the OpenAPI contract for https://app.catchallverifier.com/api/v1. API Settings keys go directly in Authorization. Added credit-balance GET and single-verification JSON POST contracts. Verification creates a paid task; retrieve its result using the returned id. Bulk verification uses /verify/bulk and can return HTTP 202 while processing.

A disposable connector-store GET /credits returned HTTP 401, Authorization information is invalid. No email was submitted or credits spent. Authenticated acceptance remains open. Focused suite: 379 passed; Ruff clean.


## Emailchef REST route

The [official integration page](https://emailchef.com/integration/) loads its [OpenAPI specification](https://emailchef.com/integration/data/openapi.yaml), version 1.4. It specifies app.emailchef.com, /apps/api/v1 resources and the authkey header. Added list GET and JSON POST coverage, including the required instance_in wrapper. A current token must be stored as the connector secret; automatic login/renewal and alternative consumerKey/consumerSecret authentication are not implemented.

A disposable connector-store GET /lists?limit=1 returned HTTP 401 with unauthorized_request. No list or subscriber was created. Authenticated acceptance remains open. Focused suite: 381 passed; Ruff clean.


## HeySummit reference availability

The [official webhook guide](https://help.heysummit.com/en/articles/11403933-how-to-set-up-webhooks-for-event-actions) documents POST /api/v2/webhooks/ and directs developers to API v2 documentation. Both https://api-v2.heysummit.com and https://api-docs.heysummit.com returned HTTP 403 during this pass. The API root returns an Event not found page. No REST route was guessed from older third-party examples.

The [official MCP guide](https://help.heysummit.com/en/articles/15921700-connect-an-ai-assistant-to-heysummit-with-mcp) confirms a separate MCP endpoint, OAuth or Token-header authentication, and paid-plan access. That does not establish the full REST contract. HeySummit remains in the 51 pending connectors. KingSumo research likewise did not locate an authoritative REST contract in this pass.


## AcyMailing customer-host REST route

The [official overview](https://docs.acymailing.com/rest-api) requires version 9.2.0+ and Essential or higher, with REST enabled in Security settings. The [users](https://docs.acymailing.com/rest-api/users) and [subscription](https://docs.acymailing.com/rest-api/subscription) endpoint references explicitly specify Api-Key with the license key and JSON writes; this conflicts with the overview authentication page calling the method Basic. This implementation follows the endpoint references.

Configure api_domain with the installation hostname. Include /index.php and the page, option, ctrl and task query parameters in the tool path; installations in a subdirectory can include it before index.php. Read pagination is appended correctly to that existing query. Subscription JSON coverage preserves arrays and false values for sendWelcomeEmail and trigger. Five malformed-host cases block transport. No live installation/key is available, so authenticated acceptance and confirmation of the documentation discrepancy remain open. Focused suite: 388 passed; Ruff clean.


## EasySendy Pro JSON REST route

The [official subscriber management reference](https://easysendy.com/email-campaigns/subscriber-management-api/) documents /rest JSON endpoints with api_key in the POST body, including POST reads. Its examples use HTTP; the configured HTTPS equivalent was verified with normal TLS validation. Added POST list retrieval and nested bulk-subscriber JSON contracts. Older /ver4 form endpoints are outside this route. Subscription writes may send confirmation emails.

A disposable connector-store POST /subscribers_list/lists with an invalid key returned HTTP 200 and {"status":"OK","count":0}, matching a direct HTTPS request. This proves host reachability only; it does not validate authentication or account data access. No subscribers were submitted. Focused suite: 390 passed; Ruff clean. Leadoku and GoZen Growth research in this pass did not establish authoritative REST host contracts; both remain pending.


## Chatrace bot-account REST route

The [official API guide](https://docs.chatrace.com/kb/chatrace-api-documentation/) links the [Swagger reference](https://api.chatrace.com/swagger/), whose swagger.json documents api.chatrace.com and X-ACCESS-TOKEN. Added mixed encoding: form for tag creation, bot fields, contact fields, numeric flow sends, payment and cart paths; JSON elsewhere. Tests cover read binding, form tags, numeric flow encoding and JSON text/contact requests. Whitelabel partner administration is separate.

A disposable connector-store GET /accounts/tags returned HTTP 401, No valid API key provided. No contacts, messages or orders were changed. Authenticated acceptance remains open. Focused suite: 395 passed; Ruff clean.


## Coupontools modern v4 route

The [official authentication overview](https://docs.coupontools.com/api/overview) distinguishes legacy client headers from modern x-api-key/x-api-secret headers. The [v4 directory reference](https://docs.coupontools.com/api/v4/directory) documents the modern host and JSON requests. Store the key and secret together as a JSON array in the connector secret store. Added a header-pair authentication style with malformed/empty/control-character validation before transport.

GET /directory and POST /directory/ID/users request contracts pass. A disposable-store live directory read with invalid credentials returned HTTP 401 Unauthorized. No users were created. This route covers modern directory/wallet APIs; legacy coupon/v3 client-header authentication is still unsupported and must not be treated as verified. Focused suite: 403 passed.


## CleverTap regional REST route

The [common API components](https://developer.clevertap.com/docs/common-api-components) document six regional hosts and the account ID/passcode header pair. Configure api_domain to match the account region and store both credentials as a JSON array. Tests cover all six hosts, [profile reads](https://developer.clevertap.com/docs/get-user-profiles-api) without Content-Type, and nested [profile upload](https://developer.clevertap.com/docs/upload-user-profiles-api) JSON with dryRun=1. The common region table uses api.clevertap.com for Europe while the profile page also lists eu1.api.clevertap.com; the hostname configuration permits either when appropriate for the account.

A disposable-store European profile GET with invalid credentials returned HTTP 400, Failed to process request. This is reachability evidence, not explicit authentication validation. Other regions have offline coverage only. No profiles were uploaded. Endpoints requiring an additional token, encrypted payloads, files or GET Content-Type (some catalogue APIs) remain unsupported by this route. Focused suite: 410 passed; Ruff clean.


## Arpoone v1.2 REST route

The [official authentication guide](https://docs.arpoone.com/docs/arpoone-api/getting-started/authentication/) specifies Bearer API keys. The [reference](https://docs.arpoone.com/api-reference/) loads [OpenAPI v1.2](https://docs.arpoone.com/services/Api/api/swagger/v1.2/swagger.json), with api.arpoone.com as server. Some balance code samples contain a .comt typo; the server definition and other official examples use .com. Added POST balance-read and short-link JSON contracts, preserving organization identifiers and nested items.

A disposable-store POST /balance/currentbalance with an invalid key and synthetic organization UUID returned HTTP 401 Unauthorized. No links, messages or balance transfers were created. Authenticated acceptance remains open. Focused suite: 412 passed; Ruff clean.


## Asters documented REST route

The [getting started guide](https://docs.asters.ai/api/overview/getting-started) specifies api.asters.ai/api/external/v1.0 and JSON headers. The [authentication guide](https://docs.asters.ai/api/overview/authentication) specifies x-api-key, as do endpoint references. Added workspace GET and POST /data/posts retrieval with nested date filters, plus GET Content-Type coverage.

A disposable-store GET /workspaces using the documented header returned HTTP 200 with data=[] and error="x-asters-key Key Not Found". This conflicts with the documented header name and does not prove the route authenticates correctly. A real key and confirmation of the current header contract are still needed. No social posts were created. Focused suite: 414 passed; Ruff clean.


## Instabot server REST route

The [official server guide](https://docs.instabot.io/docs/serverapi) specifies api.instabot.io/v1, X-Instabot-Api-Key and a master-key Authorization prefix. Added trusted catalogue prefixes to header-pair authentication. Both keys remain in the connector secret store. [Object queries](https://docs.instabot.io/docs/serverapi-objects) support GET /users?type=all and JSON POST /users/query?type=all; both have request coverage. User-session authentication and binary files are outside this route.

A disposable-store GET /users?type=all with invalid keys returned HTTP 400, API Key is invalid. This does not establish master-key acceptance. No users were created or changed. Focused suite: 416 passed; Ruff clean.

Asters follow-up: documented x-api-key alone returned x-asters-key Key Not Found; x-asters-key alone returned API key is missing; both invalid headers returned x-asters-key Key Not Found. All were HTTP 200. These results do not resolve the current credential contract, so its documented route remains unchanged and authentication is still open.


## Dux-Soup signed REST route

The [official API guide](https://support.dux-soup.com/article/227-the-dux-soup-api) specifies HMAC-SHA1 with Base64 output, signing GET URLs or request JSON bodies. Added signing after final outbound encoding, with automatic targeturl, millisecond timestamp and configured numeric userid for non-GET requests. Paths must contain that user ID. Caller-supplied envelope fields are rejected before transport. The fixed host permits documented remote-control/team paths; use each sub-API's actual path and method.

Tests cover exact signed URL/body bytes for GET, POST, PUT and DELETE, plus six invalid envelope cases. These method cases test transport signing, not that every tested path supports every method. A disposable-store POST to the [documented empty conversation batch](https://support.dux-soup.com/article/603-messaging-activity-api), with user ID 0 and an invalid key, returned HTTP 403 invalid token. No LinkedIn action was requested. A Turbo/Cloud account and real key are needed for authenticated acceptance. Focused suite: 426 passed; Ruff clean.

## Adhook API contract investigation

The [official API reference](https://app.adhook.io/api-doc/) loads [OpenAPI](https://app.adhook.io/api/openapi.json). Its schema documents Authorization header parameters and JSON request bodies, including GET /v1/subtenants/read and POST /v1/subtenants. It supplies neither servers nor security schemes, and does not specify the Authorization value format. Some read operations also expose an adhookToken header.

Read-only probes with an invalid credential returned: /v1/subtenants HTTP 404, /api/v1/subtenants HTTP 405, /api/v1/subtenants/read with a Bearer value HTTP 500, and /api/v1/posts with a Bearer value HTTP 400. These establish neither successful authentication nor a complete supported contract. Adhook remains pending rather than assigning a guessed authentication scheme. No account or social content was modified.

## Autoklose REST route

The [published API reference](https://www.postman.com/cloudy-space-2757/autoklose-s-public-workspace/documentation/twa9gic/autoklose-api) documents api.autoklose.com/api, an api_token URL query credential, and JSON request bodies. Added encoded token coverage for GET with repeated expand[] parameters and POST with a base64 attachment object. Binary downloads and multipart uploads remain unsupported. The [Integrations guide](https://help.autoklose.com/hc/en-us/articles/38723199985435-Integrations) explains generating and revoking API keys.

A disposable-store GET /me with an invalid key returned HTTP 401 with an explicit invalid API key error. No email or contact was created. Authenticated account acceptance remains open. Focused suite: 428 passed; Ruff clean.

## MailingBoss 5.0 REST route

The [official integration guide](https://knowledgebase.builderall.com/docs/mailingboss-5-0-api-integration/) documents member.mailingboss.com/integration/index.php with the Integration Key appended after the endpoint path. Added path_suffix authentication with percent encoding before query parameters, no Authorization header, and generic connection errors that omit credential-bearing URLs. GET /lists and POST /lists/fields with a JSON list_uid are covered through the connector registry.

A disposable-store GET /lists with an invalid token returned HTTP 404 and an empty message. This is reachability evidence only, not successful authentication or explicit credential rejection. No subscribers were created or updated. Focused suite: 431 passed; Ruff clean.

## BuySellAds Advertiser API

The [official authentication guide](https://docs.buysellads.com/advertiser-api) and [endpoint reference](https://docs.buysellads.com/advertiser-api/endpoints) specify papi.buysellads.com and the key query parameter. Added request contracts for all four documented reporting paths. The provider documents no pagination and no write operations for this API. Its ad-serving service is separate.

Disposable-store GET /lineitems for September 2020 returned HTTP 400 with response.error indicating Unauthorized, both for an invalid key and for the api_test example credential printed in the documentation. The example is not a working sandbox credential. No ads were served or modified. A private account-manager-issued key is needed for account acceptance. Focused suite: 435 passed; Ruff clean.

## Kartra inbound API

The [connection guide](https://support.kartra.com/en/articles/15369013-connecting-to-the-api) requires POST to app.kartra.com/api with app_id, api_key and api_password. The [official read sample](https://support.kartra.com/en/articles/15369051-php-sample-retrieving-data-for-a-specific-lead) uses form encoding with nested get_lead fields. Added a credential JSON object stored as one secret and injected into the form body, with credential overrides, query parameters, other paths and GET rejected. Call the request tool with path / and POST.

A disposable-store read request using synthetic invalid credentials returned HTTP 200 with status Error, type 239, and an invalid/inactive App Id message. HTTP success alone is not API success. No lead was created or changed. Real account credentials and a configured Kartra app are needed for acceptance. Focused suite: 441 passed; Ruff clean.

## Lahar / Ramper Marketing

The [vendor-hosted Lahar site](https://mkt.lahar.com.br/) identifies the product as Ramper Marketing. The [Ramper Pipeline published request](https://www.postman.com/ramperpipeline/ramper-marketing-exemplo/documentation/byfyu4g/cadastro-atualizao-de-contato) uses app.lahar.com.br/api/conversions with JSON token_api_lahar, nome_formulario and email_contato. Added that route and a bound request contract verifying the stored token is injected without mutating caller data. The older [Lahar SDK repository](https://github.com/LAHAR-APP/Lahar-Communication-Api) corroborates conversion integration but was not used to substitute its older encoding for the current JSON example.

A disposable-store POST containing only an invalid token, without any contact details, returned HTTP 200 with status erro and code 552 for missing required fields. This proves endpoint reachability, not authentication. No contact conversion was created. Production credentials and conversion acceptance remain open. Focused suite: 442 passed; Ruff clean.

## Dripcel MarTech API

The [official overview](https://dripcel.getoutline.com/s/2849f729-5450-4aa3-8cc9-50d29f9f2c74/doc/overview-FaM3SkQan1) specifies api.dripcel.com and Bearer API keys. Added bound GET /balance and nested JSON POST /contacts/search coverage. The [contact reference](https://dripcel.getoutline.com/s/2849f729-5450-4aa3-8cc9-50d29f9f2c74/doc/contacts-OmgWadaH8T) identifies search as a paid operation requiring contact.read.pii; it was tested offline only.

A disposable-store GET /balance with an invalid key returned HTTP 401, ok false and Invalid key. No paid search or message send was attempted. Real account acceptance remains open. Focused suite: 444 passed; Ruff clean.

## iContact standard API

The [vendor PHP SDK](https://github.com/icontact/icontact-api-php/blob/master/lib/iContactApi.php) specifies app.icontact.com/icp, three credential headers, Api-Version 2.2 and JSON Content-Type on reads and writes. Added a stored three-value JSON credential array and opt-in JSON array request bodies, including the tool schema. Tests cover account discovery and array-based list creation without sending a live write. Pro and Pro Select use separate APIs and are not this connector.

A disposable-store GET /a/ with three invalid credential values returned HTTP 401 with Api username invalid. Account authentication and real writes remain open. Focused suite: 446 passed; Ruff clean.

## Goodbits availability investigation

The referenced official documentation URL is https://support.goodbits.io/article/115-goodbit-api. A direct fetch failed with could not resolve host. Independent local resolver checks for goodbits.io, api.goodbits.io and support.goodbits.io all returned name-resolution errors. This is a current availability blocker, not proof of permanent shutdown. No replacement hostname or credential contract was inferred from aggregator integrations. Goodbits remains in the original 144-connector scope and pending until a supported contract and reachable host can be established.

## Exact Mails contract investigation

The current [provider website](https://exactmails.com) states that account creation is needed for detailed API documentation. Its publicly served main.012f70bf.js references exactmails.xyz:8012/api/v1 and links its account dashboard at exactmail-dashboard.vercel.app. Older third-party examples use api.exactmails.com/api/v1. The website asset establishes that these names are provider-published, but not which host is the supported integration API or its authentication contract. No API host was guessed from frontend configuration. Account-level documentation remains needed; this connector remains pending.

## Full-suite checkpoint at 109 routes

At commit e6a77c5, the full runtime suite completed with 3,582 passed, 2 skipped and 2 failed in 309.70 seconds. The failures were test_listener_user_data_dirs_reads_listening_cmdline (assert False) and test_run_session_raises_when_display_stays_bound (did not raise RuntimeError). Both names and failing assertions match the previously reproduced baseline results at c33bb96. This run introduced no additional failing tests; it is not a fully green suite or authenticated provider acceptance. A short temporary test directory was used to avoid Unix socket path length failures. The focused connector suite remains 446 passed.
