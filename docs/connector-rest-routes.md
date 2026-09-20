# REST connector implementation progress

Scope: implement the 144 catalogue connectors identified with missing REST hosts.
This is an implementation ledger, not a claim of live-account verification.

Twenty-nine routes now have offline request-contract coverage through the real connector
registry and credential store. Tests assert the outbound origin, version prefix,
credential header, query and JSON body. Credentials are never followed through
HTTP redirects. Production authenticated reads and writes remain unverified for these
twenty-nine routes. The remaining 115 connectors still need provider research and code.

## Implemented routes

Paths passed to tools are relative to the configured base. API credentials belong
in the connector secret store, never tool arguments or configuration fields.
ActiveCampaign additionally needs `api_domain` from its account settings.
GetResponse currently covers SMB accounts; MAX routing remains to be implemented.
CloudConvert currently uses its production automatic-region API, not its sandbox.

| Connector | Provider reference | Setup |
|---|---|---|
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
| `acumbamail` | Pending provider research and implementation |
| `acymailing` | Pending provider research and implementation |
| `add_to_calendar_pro` | Pending provider research and implementation |
| `adhook` | Pending provider research and implementation |
| `adrapid` | Pending provider research and implementation |
| `adroll` | Pending provider research and implementation |
| `adtraction` | Pending provider research and implementation |
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
| `benchmark_email` | Pending provider research and implementation |
| `bigmailer` | Route and offline request tests added; live verification pending |
| `botconversa` | Pending provider research and implementation |
| `brandmentions` | Pending provider research and implementation |
| `builderall_mailingboss` | Pending provider research and implementation |
| `buysellads` | Pending provider research and implementation |
| `callpage` | Pending provider research and implementation |
| `callrail` | Route and offline request tests added; live verification pending |
| `campaign_cleaner` | Pending provider research and implementation |
| `campaign_monitor` | Route and offline request tests added; live verification pending |
| `campaignhq` | Pending provider research and implementation |
| `campayn` | Pending provider research and implementation |
| `cardly` | Route and offline request tests added; live verification pending |
| `catch_all_verifier` | Pending provider research and implementation |
| `chatrace` | Pending provider research and implementation |
| `cleverreach` | Pending provider research and implementation |
| `clevertap` | Pending provider research and implementation |
| `clickfunnels` | Pending provider research and implementation |
| `cloud_convert` | Route and offline request tests added; live verification pending |
| `cometly` | Pending provider research and implementation |
| `constant_contact` | Pending provider research and implementation |
| `contentdrips` | Pending provider research and implementation |
| `convertkit` | Implemented; request-contract tests pass; production account verification pending |
| `copicake` | Implemented; request-contract tests pass; production account verification pending |
| `coupontools` | Pending provider research and implementation |
| `crowdpower` | Pending provider research and implementation |
| `curated` | Pending provider research and implementation |
| `cyberimpact` | Pending provider research and implementation |
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
| `easypromos` | Pending provider research and implementation |
| `easysendy` | Pending provider research and implementation |
| `echtpost_postcards` | Pending provider research and implementation |
| `ecologi` | Pending provider research and implementation |
| `email_on_acid` | Route and offline tests added; public sandbox authentication and read passed; production verification pending |
| `emailable` | Route and offline request tests added; live verification pending |
| `emailchef` | Pending provider research and implementation |
| `emaillistverify` | Pending provider research and implementation |
| `emailoctopus` | Implemented; request-contract tests pass; production account verification pending |
| `emailverify_io` | Pending provider research and implementation |
| `emelia` | Pending provider research and implementation |
| `encharge` | Pending provider research and implementation |
| `endorsal` | Pending provider research and implementation |
| `engage` | Pending provider research and implementation |
| `enginemailer` | Pending provider research and implementation |
| `enormail` | Pending provider research and implementation |
| `esputnik` | Implemented; request-contract tests pass; production account verification pending |
| `eventbrite` | Pending provider research and implementation |
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
| `giantcampaign` | Pending provider research and implementation |
| `gist` | Pending provider research and implementation |
| `gitter` | Pending provider research and implementation |
| `gobio_link` | Pending provider research and implementation |
| `goodbits` | Pending provider research and implementation |
| `google_ad_manager` | Pending provider research and implementation |
| `google_ads` | Pending provider research and implementation |
| `google_analytics` | Pending provider research and implementation |
| `google_calendar` | Pending provider research and implementation |
| `google_drive` | Pending provider research and implementation |
| `google_sheets` | Pending provider research and implementation |
| `gosquared` | Pending provider research and implementation |
| `gozen_growth` | Pending provider research and implementation |
| `grade_us` | Pending provider research and implementation |
| `greenspark` | Pending provider research and implementation |
| `growsurf` | Route and offline request tests added; live verification pending |
| `herobot` | Pending provider research and implementation |
| `heysummit` | Pending provider research and implementation |
| `hippo_video` | Pending provider research and implementation |
| `humanitix` | Pending provider research and implementation |
| `hypeauditor` | Pending provider research and implementation |
| `hyperise` | Pending provider research and implementation |
| `icontact` | Pending provider research and implementation |
| `impression` | Pending provider research and implementation |
| `indiefunnels` | Pending provider research and implementation |
| `infusionsoft` | Pending provider research and implementation |
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
| `klenty` | Pending provider research and implementation |
| `kyvio` | Pending provider research and implementation |
| `lagrowthmachine` | Pending provider research and implementation |
| `lahar` | Pending provider research and implementation |
| `laposta` | Pending provider research and implementation |
| `lawmatics` | Pending provider research and implementation |
| `lead_identity_check` | Pending provider research and implementation |
| `leaddyno` | Pending provider research and implementation |
| `leadoku` | Pending provider research and implementation |
| `leadpops` | Pending provider research and implementation |
| `linkedin` | Pending provider research and implementation |
| `microsoft_excel` | Pending provider research and implementation |
| `microsoft_outlook` | Pending provider research and implementation |
| `microsoft_teams` | Pending provider research and implementation |

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
