# REST connector implementation progress

Scope: implement the 144 catalogue connectors identified with missing REST hosts.
This is an implementation ledger, not a claim of live-account verification.

Fourteen routes now have offline request-contract coverage through the real connector
registry and credential store. Tests assert the outbound origin, version prefix,
credential header, query and JSON body. Credentials are never followed through
HTTP redirects. Live authenticated reads and writes remain unverified for these
fourteen routes. The remaining 130 connectors still need provider research and code.

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

Current focused validation: 186 tests pass across REST routes, generic connectors,
redaction, connector catalogue/tools, mentions and public export. Ruff passes.

Second-batch invalid-credential probes: 360NRS, ActiveTrail, Campaign Monitor,
Drip and Cardly returned HTTP 401; 4Dem rejected its authentication exchange with
HTTP 401; BigMailer returned HTTP 400 with an invalid API-key error. No authenticated
account access is claimed. Cardly JSON content-type handling is also covered.

## Full 144-connector ledger

| Connector | Implementation status |
|---|---|
| `360nrs` | Route and offline request tests added; live verification pending |
| `4dem` | Route and offline request tests added; live verification pending |
| `abyssale` | Route and offline request tests added; live verification pending |
| `acelle_mail` | Pending provider research and implementation |
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
| `beamer` | Pending provider research and implementation |
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
| `convertkit` | Pending provider research and implementation |
| `copicake` | Pending provider research and implementation |
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
| `dropcontact` | Pending provider research and implementation |
| `dux_soup` | Pending provider research and implementation |
| `dynamic_content_snippet` | Pending provider research and implementation |
| `dynapictures` | Pending provider research and implementation |
| `egoi` | Pending provider research and implementation |
| `easypromos` | Pending provider research and implementation |
| `easysendy` | Pending provider research and implementation |
| `echtpost_postcards` | Pending provider research and implementation |
| `ecologi` | Pending provider research and implementation |
| `email_on_acid` | Pending provider research and implementation |
| `emailable` | Pending provider research and implementation |
| `emailchef` | Pending provider research and implementation |
| `emaillistverify` | Pending provider research and implementation |
| `emailoctopus` | Pending provider research and implementation |
| `emailverify_io` | Pending provider research and implementation |
| `emelia` | Pending provider research and implementation |
| `encharge` | Pending provider research and implementation |
| `endorsal` | Pending provider research and implementation |
| `engage` | Pending provider research and implementation |
| `enginemailer` | Pending provider research and implementation |
| `enormail` | Pending provider research and implementation |
| `esputnik` | Pending provider research and implementation |
| `eventbrite` | Pending provider research and implementation |
| `everwebinar` | Pending provider research and implementation |
| `exact_mails` | Pending provider research and implementation |
| `facebook` | Pending provider research and implementation |
| `feedblitz` | Pending provider research and implementation |
| `flexmail` | Pending provider research and implementation |
| `flippingbook` | Pending provider research and implementation |
| `fomo` | Pending provider research and implementation |
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
| `growsurf` | Pending provider research and implementation |
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
| `instasent` | Pending provider research and implementation |
| `jellyreach` | Pending provider research and implementation |
| `joggai` | Pending provider research and implementation |
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
