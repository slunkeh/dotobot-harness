"""Connectors: UI-managed integrations (Slack, GitHub, Google, ...).

The harness stores connector *configuration* (non-secret fields on the shared
volume, secrets in the credentials store). Connector configuration is fully
UI-driven. The tool runtime that lets a bot *use* a connector lives in the
top-level `connectors/` package (mirroring `providers/`); types with a runtime
report their tool names in the catalog so clients can show what a connector
adds.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .connector_docs import DOCS
from .connector_stubs import STUBS
from .paths import HarnessPaths
from .secrets import delete_secret, secret_source, set_secret

WORKSPACE_TYPES = frozenset({"gmail", "google_calendar", "google_drive", "google_sheets", "google_docs"})

# Available connector types the UI can offer, with the gallery metadata a
# client needs to render a plugin-store style browser. `auth` is how
# credentials are provided; `fields` are non-secret config inputs; `icon` is
# an SF Symbol name used when the client has no Plugin<Type>.svg/.png.
# `mcp_url` marks a service with a first-party remote MCP server: the user
# connects it with an OAuth consent page (harness/mcp_oauth.py) and the tool
# surface comes from the server itself (connectors/mcp.py). An api-key
# runtime, where one exists, stays as the fallback until OAuth is connected.
CATALOG: list[dict] = [
    {
        "type": "linear",
        "name": "Linear",
        "auth": "api_key",
        "mcp_url": "https://mcp.linear.app/mcp",
        "fields": [],
        "category": "Project Management",
        "description": "Search, create, and update Linear issues, set a project, and attach chat files.",
        "icon": "list.bullet.rectangle",
    },
    {
        "type": "slack",
        "name": "Slack",
        "auth": "oauth",
        "mcp_url": "https://mcp.slack.com/mcp",
        "fields": ["client_id"],
        "oauth_scopes": ["search:read.public", "channels:read", "channels:history", "chat:write"],
        "category": "Inbox And Collaboration",
        "description": "Search public Slack channels and messages, read public-channel history, and send messages. Slack needs a Slack app (Client ID and Client Secret); it does not register automatically.",
        "icon": "number",
    },
    {
        "type": "github",
        "name": "GitHub",
        "auth": "api_key",
        "mcp_url": "https://api.githubcopilot.com/mcp/",
        "prefer_static": True,
        "fields": ["org"],
        "category": "Developer",
        "description": (
            "Browse repository files, issues, and pull requests. Paste a GitHub personal "
            "access token (classic ghp_ or fine-grained github_pat_)."
        ),
        "icon": "chevron.left.forwardslash.chevron.right",
    },
    {
        "type": "notion",
        "name": "Notion",
        "auth": "oauth",
        "mcp_url": "https://mcp.notion.com/mcp",
        "fields": [],
        "category": "Productivity",
        "description": "Read and write pages in your Notion workspace.",
        "icon": "doc.text",
    },
    {
        "type": "cloudflare",
        "name": "Cloudflare",
        "auth": "api_key",
        "mcp_url": "https://mcp.cloudflare.com/mcp",
        "fields": [],
        "category": "Infrastructure",
        "description": "Manage DNS, Workers, and Pages on Cloudflare.",
        "icon": "cloud",
    },
    {
        "type": "stripe",
        "name": "Stripe",
        "auth": "oauth",
        "mcp_url": "https://mcp.stripe.com",
        "fields": [],
        "category": "Finance And Payments",
        "description": "Look up payments, customers, subscriptions, and invoices in Stripe.",
        "icon": "creditcard",
    },
    {
        "type": "paypal",
        "name": "PayPal",
        "auth": "oauth",
        "mcp_url": "https://mcp.paypal.com/mcp",
        "fields": [],
        "category": "Finance And Payments",
        "description": "Read PayPal orders, invoices, and disputes.",
        "icon": "creditcard.circle",
    },
    {
        "type": "square",
        "name": "Square",
        "auth": "oauth",
        "mcp_url": "https://mcp.squareup.com/mcp",
        "fields": [],
        "category": "Finance And Payments",
        "description": "Query Square payments, merchants, and catalog data.",
        "icon": "square.grid.2x2",
    },
    {
        "type": "hubspot",
        "name": "HubSpot",
        "auth": "oauth",
        "mcp_url": "https://mcp.hubspot.com/anthropic",
        "fields": ["client_id"],
        "category": "Sales And CRM",
        "description": "Search and update HubSpot contacts, companies, and deals.",
        "icon": "person.2",
    },
    {
        "type": "atlassian",
        "name": "Atlassian",
        "auth": "oauth",
        "mcp_url": "https://mcp.atlassian.com/v1/mcp",
        "fields": [],
        "category": "Project Management",
        "description": "Work Jira issues and Confluence pages through Atlassian Rovo.",
        "icon": "square.stack.3d.up",
    },
    {
        "type": "asana",
        "name": "Asana",
        "auth": "oauth",
        "mcp_url": "https://mcp.asana.com/v2/mcp",
        "fields": ["client_id"],
        "category": "Project Management",
        "description": "Coordinate Asana tasks, projects, and portfolios. Asana needs a registered MCP app (Client ID and Client Secret) with access to your workspace; it does not register automatically.",
        "icon": "checklist",
    },
    {
        "type": "intercom",
        "name": "Intercom",
        "auth": "oauth",
        "mcp_url": "https://mcp.intercom.com/mcp",
        "fields": [],
        "category": "Customer Support",
        "description": "Search Intercom conversations and contacts.",
        "icon": "bubble.left.and.bubble.right",
    },
    {
        "type": "sentry",
        "name": "Sentry",
        "auth": "oauth",
        "mcp_url": "https://mcp.sentry.dev/mcp",
        "fields": [],
        "category": "Developer",
        "description": "Triage Sentry issues, releases, and stack traces.",
        "icon": "exclamationmark.triangle",
    },
    {
        "type": "vercel",
        "name": "Vercel",
        "auth": "oauth",
        "mcp_url": "https://mcp.vercel.com",
        "fields": [],
        "category": "Developer",
        "description": "Inspect Vercel projects, deployments, and build logs.",
        "icon": "triangle",
    },
    {
        "type": "posthog",
        "name": "PostHog",
        "auth": "oauth",
        "mcp_url": "https://mcp.posthog.com/mcp",
        "fields": [],
        "category": "Analytics",
        "description": "Run PostHog queries and manage insights and feature flags.",
        "icon": "chart.line.uptrend.xyaxis",
    },
    {
        "type": "amplitude",
        "name": "Amplitude",
        "auth": "oauth",
        "mcp_url": "https://mcp.amplitude.com/mcp",
        "fields": [],
        "category": "Analytics",
        "description": "Query Amplitude charts, dashboards, and experiments.",
        "icon": "chart.bar.xaxis",
    },
    {
        "type": "monday",
        "name": "monday.com",
        "auth": "oauth",
        "mcp_url": "https://mcp.monday.com/mcp",
        "fields": [],
        "category": "Project Management",
        "description": "Manage monday.com boards, items, and workflows.",
        "icon": "square.grid.3x3",
    },
    {
        "type": "airtable",
        "name": "Airtable",
        "auth": "oauth",
        "mcp_url": "https://mcp.airtable.com/mcp",
        "fields": [],
        "category": "Productivity",
        "description": "Read and write Airtable bases, tables, and records.",
        "icon": "tablecells",
    },
    {
        "type": "miro",
        "name": "Miro",
        "auth": "oauth",
        "mcp_url": "https://mcp.miro.com/",
        "fields": [],
        "category": "Design And Content",
        "description": "Read and create items on Miro boards.",
        "icon": "rectangle.3.group",
    },
    {
        # Zoom publishes one server per surface (docs, tasks, chat, revenue
        # accelerator). Meetings is the one worth a bot's default.
        "type": "zoom",
        "name": "Zoom",
        "auth": "oauth",
        "mcp_url": "https://mcp.zoom.us/mcp/meeting/streamable",
        "fields": [],
        "category": "Inbox And Collaboration",
        "description": "Search Zoom meetings, recordings, and summaries.",
        "icon": "video",
    },
    {
        "type": "figma",
        "name": "Figma",
        "auth": "oauth",
        "mcp_url": "https://mcp.figma.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Pull design context, screenshots, and variables out of Figma.",
        "icon": "pencil.and.outline",
    },
    {
        "type": "canva",
        "name": "Canva",
        "auth": "oauth",
        "mcp_url": "https://mcp.canva.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Search, create, and export Canva designs.",
        "icon": "paintpalette",
    },
    {
        "type": "webflow",
        "name": "Webflow",
        "auth": "oauth",
        "mcp_url": "https://mcp.webflow.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Build pages, manage CMS collections, and publish Webflow sites.",
        "icon": "macwindow",
    },
    {
        # Shopify runs the server on the merchant's own store, so the URL is
        # per-tenant: the operator supplies `store_domain` and `mcp_url` fills
        # the template in. Until they do, this stays on the sign-in stub.
        "type": "shopify",
        "name": "Shopify",
        "auth": "oauth",
        "mcp_url_template": "https://{store_domain}/api/mcp",
        "fields": ["store_domain"],
        "category": "E-Commerce",
        "description": "Manage Shopify products, orders, and customers.",
        "icon": "bag",
    },
    {
        "type": "zapier",
        "name": "Zapier",
        "auth": "oauth",
        "mcp_url": "https://mcp.zapier.com/api/v1/connect",
        "fields": [],
        "category": "Productivity",
        "description": "Trigger Zaps across thousands of connected apps.",
        "icon": "bolt",
    },
    {
        # Composio Connect is the shared Streamable-HTTP MCP URL (OAuth at
        # login.composio.dev). Per-user SDK session URLs stay out of the
        # catalog — this is the one any MCP client can point at.
        "type": "composio",
        "name": "Composio",
        "auth": "oauth",
        "mcp_url": "https://connect.composio.dev/mcp",
        "fields": [],
        "category": "Productivity",
        "description": "Search, authorize, and run tools across 1000+ apps.",
        "icon": "point.3.connected.trianglepath",
    },
    {
        "type": "granola",
        "name": "Granola",
        "auth": "oauth",
        "mcp_url": "https://mcp.granola.ai/mcp",
        "fields": [],
        "category": "Inbox And Collaboration",
        "description": "Search Granola meeting notes, action items, and decisions.",
        "icon": "note.text",
    },
    {
        "type": "ahrefs",
        "name": "Ahrefs",
        "auth": "oauth",
        "mcp_url": "https://api.ahrefs.com/mcp/mcp",
        "fields": [],
        "category": "Analytics",
        "description": "Keyword research, backlinks, rank tracking, and site audits from Ahrefs.",
        "icon": "chart.bar.xaxis",
    },
    {
        # Gmail uses delegated short-lived OAuth access with IMAP/SMTP XOAUTH2.
        "type": "gmail",
        "name": "Gmail",
        "auth": "oauth",
        "fields": [],
        "multi_account": True,
        "category": "Inbox And Collaboration",
        "description": (
            "Search threads, read mail and attachments, create drafts, and send email in Gmail."
        ),
        "icon": "envelope",
    },
    {
        # n8n serves MCP on the operator's own instance, so the URL is
        # per-tenant: the operator supplies `instance_host` and `mcp_url`
        # fills the template in. Until they do, this stays on the sign-in stub.
        "type": "n8n",
        "name": "n8n",
        "auth": "oauth",
        "mcp_url_template": "https://{instance_host}/mcp-server/http",
        "fields": ["instance_host"],
        "category": "Productivity",
        "description": "Run and inspect n8n workflows on your instance.",
        "icon": "arrow.triangle.branch",
    },
    {
        # Official MCP is stdio (`npx trigger.dev@latest mcp`). No hosted
        # Streamable HTTP URL is published, so this stays on the sign-in stub
        # until they ship a remote endpoint (then it is one `mcp_url` line).
        "type": "triggerdev",
        "name": "Trigger.dev",
        "auth": "oauth",
        "fields": [],
        "category": "Developer",
        "description": "Trigger and monitor Trigger.dev tasks and deploys.",
        "icon": "bolt.horizontal",
    },
    {
        # Official Environments MCP is local stdio (`1password-mcp` from the
        # desktop app). Remote-only clients are not supported; no hosted
        # Streamable HTTP URL is published, so this stays on the sign-in stub
        # until they ship a remote endpoint (then it is one `mcp_url` line).
        # www.1password.dev/mcp is Mintlify docs search, not vaults.
        "type": "1password",
        "name": "1Password",
        "auth": "oauth",
        "fields": [],
        "category": "Productivity",
        "description": "Manage 1Password Environments and secret names without exposing values.",
        "icon": "lock.fill",
    },
    # Salesforce and QuickBooks have a first-party MCP server in Claude's
    # connector directory, but neither vendor has published an endpoint to
    # registry.modelcontextprotocol.io and their hosts are unreachable from
    # here, so there is no URL to put in either entry that anyone has checked.
    # They store config and show the Authorize card. Once someone confirms the
    # endpoint, each is one `mcp_url` (or `mcp_url_template`) line.
    {
        "type": "salesforce",
        "name": "Salesforce",
        "auth": "oauth",
        "fields": ["instance_domain"],
        "api_base_template": "https://{instance_domain}",
        "category": "Sales And CRM",
        "description": "Query and update Salesforce records.",
        "icon": "person.text.rectangle",
    },
    {
        "type": "quickbooks",
        "name": "QuickBooks",
        "auth": "oauth",
        "fields": ["realm_id"],
        "category": "Finance And Payments",
        "description": "Read QuickBooks ledgers, invoices, and cash-flow reports.",
        "icon": "dollarsign.circle",
    },
    {
        "type": "openai",
        "name": "OpenAI (ChatGPT)",
        "auth": "oauth",
        "fields": [],
        "category": "Developer",
        "description": "Use ChatGPT and the OpenAI API from a bot.",
        "icon": "sparkles",
    },
    {
        "type": "anthropic",
        "name": "Anthropic (Claude)",
        "auth": "oauth",
        "fields": [],
        "category": "Developer",
        "description": "Use Claude and the Anthropic API from a bot.",
        "icon": "sparkles",
    },
    {
        "type": "calendly",
        "name": "Calendly",
        "auth": "oauth",
        "mcp_url": "https://mcp.calendly.com",
        "fields": [],
        "category": "Productivity",
        "description": "List event types, check availability, and manage scheduled events.",
        "icon": "square.grid.2x2",
    },
    {
        "type": "clickup",
        "name": "ClickUp",
        "auth": "oauth",
        "mcp_url": "https://mcp.clickup.com/mcp",
        "fields": [],
        "category": "Project Management",
        "description": "Manage ClickUp tasks, lists, docs, and time tracking.",
        "icon": "checklist",
    },
    {
        "type": "dropbox",
        "name": "Dropbox",
        "auth": "oauth",
        "mcp_url": "https://mcp.dropbox.com/mcp",
        "fields": [],
        "category": "Productivity",
        "description": "Search, read, and manage files in Dropbox.",
        "icon": "folder",
    },
    {
        "type": "trello",
        "name": "Trello",
        "auth": "oauth",
        "mcp_url": "https://mcp.trello.com/v1",
        "fields": [],
        "category": "Project Management",
        "description": "Manage Trello boards, lists, cards, and checklists.",
        "icon": "checklist",
    },
    {
        "type": "typeform",
        "name": "Typeform",
        "auth": "oauth",
        "mcp_url": "https://api.typeform.com/mcp",
        "fields": [],
        "category": "Productivity",
        "description": "Build forms, manage contacts, and analyse Typeform responses.",
        "icon": "square.grid.2x2",
    },
    {
        "type": "jira",
        "name": "Jira",
        "auth": "oauth",
        "mcp_url": "https://mcp.atlassian.com/v1/mcp",
        "fields": [],
        "category": "Project Management",
        "description": "Work Jira issues and Confluence pages through Atlassian Rovo.",
        "icon": "checklist",
    },
    {
        "type": "klaviyo",
        "name": "Klaviyo",
        "auth": "oauth",
        "mcp_url": "https://mcp.klaviyo.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Report on campaigns, profiles, segments, and flows in Klaviyo.",
        "icon": "envelope",
    },
    {
        "type": "beehiiv",
        "name": "beehiiv",
        "auth": "oauth",
        "mcp_url": "https://mcp.beehiiv.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Read beehiiv publications, subscribers, and performance.",
        "icon": "envelope",
    },
    {
        "type": "bitly",
        "name": "Bitly",
        "auth": "oauth",
        "mcp_url": "https://api-ssl.bitly.com/v4/mcp",
        "fields": [],
        "category": "Developer",
        "description": "Create short links, QR codes, and inspect Bitly analytics.",
        "icon": "link",
    },
    {
        "type": "braze",
        "name": "Braze",
        "auth": "oauth",
        "mcp_url": "https://mcp.braze.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Query campaigns, canvases, and messaging analytics in Braze.",
        "icon": "envelope",
    },
    {
        "type": "customer_io",
        "name": "Customer.io",
        "auth": "oauth",
        "mcp_url": "https://mcp.customer.io/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Search and manage Customer.io people, campaigns, and messages.",
        "icon": "envelope",
    },
    {
        "type": "contentful",
        "name": "Contentful",
        "auth": "oauth",
        "mcp_url": "https://mcp.contentful.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Read and write Contentful spaces, entries, and content types.",
        "icon": "paintbrush",
    },
    {
        "type": "instantly",
        "name": "Instantly",
        "auth": "oauth",
        "mcp_url": "https://mcp.instantly.ai/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Manage Instantly campaigns, leads, and outreach.",
        "icon": "envelope",
    },
    {
        "type": "ez_texting",
        "name": "EZ Texting",
        "auth": "oauth",
        "mcp_url": "https://mcp.eztexting.com/mcp",
        "fields": [],
        "category": "Inbox And Collaboration",
        "description": "Send and inspect EZ Texting SMS campaigns.",
        "icon": "bubble.left.and.bubble.right",
    },
    {
        "type": "frontify",
        "name": "Frontify",
        "auth": "oauth",
        "mcp_url": "https://mcp.frontify-integrations.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Search Frontify brand assets and guidelines.",
        "icon": "paintbrush",
    },
    {
        "type": "handwrytten",
        "name": "Handwrytten",
        "auth": "oauth",
        "mcp_url": "https://mcp.handwrytten.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Send handwritten notes through Handwrytten.",
        "icon": "envelope",
    },
    {
        "type": "front",
        "name": "Front",
        "auth": "oauth",
        "mcp_url": "https://mcp.frontapp.com/mcp",
        "fields": [],
        "category": "Inbox And Collaboration",
        "description": "Search and reply to Front inboxes and conversations.",
        "icon": "bubble.left.and.bubble.right",
    },
    {
        "type": "aweber",
        "name": "AWeber",
        "auth": "oauth",
        "mcp_url": "https://mcp.aweber.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Manage AWeber lists, subscribers, and broadcasts.",
        "icon": "envelope",
    },
    {
        "type": "hygraph",
        "name": "Hygraph",
        "auth": "oauth",
        "mcp_url": "https://mcp.hygraph.com/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Query Hygraph content and schema.",
        "icon": "paintbrush",
    },
    {
        "type": "fullenrich",
        "name": "FullEnrich",
        "auth": "oauth",
        "mcp_url": "https://mcp.fullenrich.com/mcp",
        "fields": [],
        "category": "Sales And CRM",
        "description": "Enrich B2B contacts and companies via FullEnrich.",
        "icon": "person.2",
    },
    {
        "type": "brandfetch",
        "name": "Brandfetch",
        "auth": "oauth",
        "mcp_url": "https://mcp.brandfetch.io/mcp",
        "fields": [],
        "category": "Design And Content",
        "description": "Search brand logos, colors, fonts, and company details.",
        "icon": "paintbrush",
    },
    {
        "type": "amazon_advertising",
        "name": "Amazon Advertising",
        "auth": "oauth",
        "mcp_url": "https://advertising-ai.amazon.com/mcp",
        "fields": [],
        "category": "Analytics",
        "description": "Manage Amazon Ads campaigns, reports, and billing.",
        "icon": "chart.bar.xaxis",
    },
    {
        "type": "dub",
        "name": "Dub",
        "auth": "oauth",
        "mcp_url": "https://mcp.dub.sh/mcp/dub-partners",
        "fields": [],
        "category": "Developer",
        "description": "Manage Dub partner programs, links, and commissions.",
        "icon": "link",
    },
    {
        "type": "elastic_email",
        "name": "Elastic Email",
        "auth": "oauth",
        "mcp_url": "https://mcp.elasticemail.com/mcp",
        "fields": [],
        "category": "Email And Marketing",
        "description": "Send and inspect Elastic Email campaigns.",
        "icon": "envelope",
    },
    {
        "type": "cufinder",
        "name": "CUFinder",
        "auth": "oauth",
        "mcp_url": "https://mcp.cufinder.io/mcp",
        "fields": [],
        "category": "Sales And CRM",
        "description": "Look up B2B contacts and companies in CUFinder.",
        "icon": "person.2",
    },
    {
        "type": "heyreach",
        "name": "HeyReach",
        "auth": "oauth",
        "mcp_url": "https://mcp.heyreach.io/mcp",
        "fields": [],
        "category": "Sales And CRM",
        "description": "Run HeyReach LinkedIn outreach campaigns.",
        "icon": "person.2",
    },
    {
        "type": "xero",
        "name": "Xero Accounting",
        "auth": "oauth",
        "mcp_url": "https://mcp.xero.com/mcp",
        "fields": [],
        "category": "Finance And Payments",
        "description": "Read Xero invoices, contacts, and reports.",
        "icon": "dollarsign.circle",
    },
    {
        "type": "pipedrive",
        "name": "Pipedrive",
        "auth": "oauth",
        "mcp_url": "https://mcp.pipedrive.ai/mcp",
        "fields": [],
        "category": "Sales And CRM",
        "description": "Search and update Pipedrive deals, people, and orgs.",
        "icon": "person.2",
    },
    {
        "type": "zendesk",
        "name": "Zendesk",
        "auth": "api_key",
        "fields": ["subdomain"],
        "api_base_template": "https://{subdomain}.zendesk.com/api/v2",
        "category": "Customer Support",
        "description": "Read and update Zendesk tickets and users.",
        "icon": "questionmark.circle",
    },
    {
        "type": "woocommerce",
        "name": "WooCommerce",
        "auth": "api_key",
        "fields": ["store_domain"],
        "api_base_template": "https://{store_domain}/wp-json/wc/v3",
        "auth_style": "basic",
        "category": "E-Commerce",
        "description": "Manage WooCommerce products, orders, and customers.",
        "icon": "bag",
    },
    *STUBS,
]

# Google API consent is independent of remote MCP discovery.

for _entry in CATALOG:
    if _entry["type"] in WORKSPACE_TYPES:
        _entry.update(auth="oauth", oauth_supported=True, multi_account=True, prefer_static=True, auth_broker="control_plane")

_CATALOG_TYPES = {c["type"]: c for c in CATALOG}

# Gallery Featured row — popular apps first, matching the plugin-store
# screenshot (generic API/MCP tiles are not catalog types). Rank is the
# index; catalog() stamps `featured` / `featured_rank` so clients sort
# without a second list.
FEATURED: list[str] = [
    "slack",
    "gmail",
    "google_sheets",
    "google_calendar",
    "google_drive",
    "hubspot",
    "salesforce",
    "notion",
    "microsoft_teams",
    "microsoft_outlook",
    "microsoft_excel",
    "openai",
    "anthropic",
    "stripe",
    "shopify",
    "github",
    "linear",
    "jira",
    "asana",
    "trello",
    "zoom",
    "mailchimp",
    "airtable",
    "zendesk",
    "intercom",
    "twilio",
    "calendly",
    "typeform",
    "whatsapp",
    "telegram",
    "ahrefs",
    "linkedin",
    "monday",
    "clickup",
    "pipedrive",
    "quickbooks",
    "xero",
    "woocommerce",
    "dropbox",
]

_UNSET = object()


def runtime_tool_names(type_: str) -> list[str]:
    """Tool names the runtime package provides for a type ([] = config only)."""
    try:
        from connectors.registry import tool_names
    except ImportError:  # runtime package absent (partial deploy)
        return []
    return tool_names(type_)


#: Extra @mention titles for a catalog type. Google is one connector that
#: people address as Gmail / Drive / Calendar (same as the Mac/iOS picker).
_MENTION_ALIASES = {
    "google": ("Google Drive", "Google Calendar"),
}
_MENTION_TAIL = re.compile(r"[A-Za-z0-9_-]")


def mention_titles(type_: str, name: str) -> tuple[str, ...]:
    """Titles a user might @mention for this connector (catalog name first)."""
    titles: list[str] = []
    for title in (name, *(_MENTION_ALIASES.get(type_, ())), type_):
        t = str(title or "").strip()
        if t and t.lower() not in {x.lower() for x in titles}:
            titles.append(t)
    return tuple(titles)


def is_connected_record(record: Mapping[str, Any]) -> bool:
    """True when the user has added this connector (OAuth or a stored secret)."""
    return bool(record.get("oauth_configured") or record.get("secret_configured"))


def _at_mentions(text: str, title: str) -> bool:
    """True when `@title` appears in `text` (case-insensitive, name-bounded)."""
    if not title:
        return False
    hay = (text or "").lower()
    needle = "@" + title.lower()
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        end = i + len(needle)
        if end == len(hay) or not _MENTION_TAIL.match(hay[end]):
            return True
        start = end


def mentioned_connected(text: str, records: list[dict]) -> list[dict]:
    """Connected connector records the user @mentioned in this turn."""
    text = instruction_text(text)
    selected = {m["connector_id"] for m in connector_scope_delta(text, records)["matches"]}
    out: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        if not is_connected_record(rec):
            continue
        cid = str(rec.get("id") or "")
        if cid not in selected or cid in seen:
            continue
        titles = mention_titles(str(rec.get("type") or ""), str(rec.get("name") or ""))
        if _at_mentions(text, f"connector:{cid}") or any(
            _at_mentions(text, title) for title in titles
        ):
            if cid:
                seen.add(cid)
            out.append(rec)
    return out


def mentioned_unconnected(text: str, records: list[dict]) -> list[dict]:
    """Added-but-not-signed-in connectors the user @mentioned or named.

    Grok Bot lists these in the `@` picker as "needs auth" and the bot's
    answer is the sign-in card, not "I don't have that". The turn holds only
    the `<type>_connect` stub for such a record (the registry binds nothing
    else before OAuth), so offering it is how the card gets shown. Intent
    hints never reach here — a generic word must not surface a plugin the
    user has not finished adding.
    """
    out: list[dict] = []
    seen: set[str] = set()
    selected = {m["connector_id"] for m in connector_scope_delta(text, records)["matches"]}
    for rec in records:
        if is_connected_record(rec):
            continue
        cid = str(rec.get("id") or "")
        if cid and cid in seen:
            continue
        if cid in selected:
            if cid:
                seen.add(cid)
            out.append(rec)
    return out


def named_catalog_types(text: str, records: list[dict]) -> list[dict]:
    """Catalog entries the user named that have no connector record at all.

    "Using Notion, list my pages" with no Notion added: the right reply is an
    offer to add it (`add_connector`), never a roster bot named Notion. A
    type is skipped once any record of it exists — connected or not, those
    are `relevant_connected` / `mentioned_unconnected` territory.
    """
    added = {str(rec.get("type") or "") for rec in records}
    selected = {
        m["connector_id"]
        for m in connector_scope_delta(
            text, [{**cat, "id": cat["type"]} for cat in CATALOG if cat.get("type")]
        )["matches"]
    }
    out: list[dict] = []
    for cat in CATALOG:
        type_ = str(cat.get("type") or "")
        if not type_ or type_ in added:
            continue
        if type_ in selected:
            out.append(cat)
    return out


_WORD_EDGE = re.compile(r"[a-z0-9_]")


def _word_in(text: str, phrase: str) -> bool:
    """True when `phrase` appears in `text` on word boundaries (case-insensitive)."""
    hay = (text or "").lower()
    needle = (phrase or "").strip().lower()
    if not needle:
        return False
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        end = i + len(needle)
        before_ok = i == 0 or not _WORD_EDGE.match(hay[i - 1])
        after_ok = end == len(hay) or not _WORD_EDGE.match(hay[end])
        if before_ok and after_ok:
            return True
        start = i + 1


def instruction_text(text: str) -> str:
    """Remove marked quotations before considering connector names.

    This is conservative source filtering, not a natural-language classifier.
    Unmarked pasted prose has no recoverable provenance; callers must continue
    to keep attachment/quote/tool-result fields separate from human input.
    Backtick tool identifiers remain usable; fenced code and quoted prose do not.
    """
    text = re.sub(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)", " ", text or "")
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’', " ", text)
    return re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", text)


_SCOPE_CLAUSE = re.compile(r"(?<=[.!?])\s+|[;\n]|\bbut\b", re.IGNORECASE)
_SCOPE_NEGATIVE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|avoid|exclude|without|stop\s+(?:using|loading)|"
    r"no(?:\s+more)?|not|neither)\s+"
    r"(?:(?:want|need)(?:\s+to)?\s+)?"
    r"(?:(?:use|using|load|loading|access|accessing|add|adding|connect(?:ing)?(?:\s+to)?)\s+)?"
    r"(?:(?:the|either)\s+)?[@`]*$",
    re.IGNORECASE,
)
_SCOPE_LIST_SEPARATOR = re.compile(r"(?:\s|[,/&@`]|and\b|or\b|nor\b)+", re.IGNORECASE)


def _negative_scope_names(clause: str, names: list[str]) -> set[str]:
    """Negation binds a named target (or name list), never an entire clause."""
    if not names:
        return set()
    pattern = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    excluded = set()
    previous_end = 0
    previous_negative = False
    for match in re.finditer(r"(?<![a-z0-9_])(?:" + pattern + r")(?![a-z0-9_])", clause, re.I):
        negative = bool(_SCOPE_NEGATIVE.search(clause[: match.start()])) or (
            previous_negative
            and bool(_SCOPE_LIST_SEPARATOR.fullmatch(clause[previous_end : match.start()]))
        )
        if negative:
            excluded.add(match.group().lower())
        previous_end, previous_negative = match.end(), negative
    return excluded


def connector_scope_delta(text: str, records: list[dict]) -> dict[str, Any]:
    """Explicit human connector names, provenance matches, and exclusions.

    Account names and actual account-prefixed tool names take precedence over
    their shared service alias. Exclusions win within one input. This function
    never fetches tool schemas and never interprets generic task vocabulary.
    """
    from collections import Counter

    from connectors.registry import tool_prefixes

    clean = instruction_text(text)
    prefixes = tool_prefixes(records, Counter(str(r.get("type") or "") for r in records))
    matches: dict[str, dict] = {}
    excluded: set[str] = set()
    for clause in _SCOPE_CLAUSE.split(clean):
        candidates: list[tuple[dict, str, bool]] = []
        for rec in records:
            cid = str(rec.get("id") or "")
            type_ = str(rec.get("type") or "")
            if not cid:
                continue
            generic = mention_titles(type_, "")
            account = str(rec.get("name") or "")
            specific = (
                [account] if account and account.lower() not in {t.lower() for t in generic} else []
            )
            prefix = prefixes.get(cid, type_)
            for name in rec.get("tools", []):
                name = str(name)
                if name.startswith(type_ + "_") and not name.startswith(prefix + "_"):
                    name = prefix + name[len(type_) :]
                specific.append(name)
            identity = f"connector:{cid}"
            if _at_mentions(clause, identity):
                specific.insert(0, identity)
            exact = next((name for name in specific if name and _word_in(clause, name)), "")
            broad = next((name for name in generic if _word_in(clause, name)), "")
            if exact or broad:
                candidates.append((rec, exact or broad, bool(exact)))
        specific_types = {str(r.get("type")) for r, _, exact in candidates if exact}
        negative_names = _negative_scope_names(clause, [name for _, name, _ in candidates])
        for rec, matched, exact in candidates:
            if not exact and str(rec.get("type")) in specific_types:
                continue
            cid = str(rec["id"])
            if matched.lower() in negative_names:
                excluded.add(cid)
            else:
                matches[cid] = {"connector_id": cid, "matched_name": matched}
    for cid in excluded:
        matches.pop(cid, None)
    return {"matches": list(matches.values()), "excluded_ids": sorted(excluded)}


def relevant_connected(text: str, records: list[dict], *, persona: str = "") -> list[dict]:
    """Select only connectors explicitly named in trusted user chat text.

    Names, @mentions and known tool names qualify. Generic task words and
    bot personas never do. The caller must pass the user's original text,
    excluding quotes, attachments, skills, tool results and generated work.
    `persona` remains accepted for callers using the old signature.
    """
    del persona
    selected = {m["connector_id"] for m in connector_scope_delta(text, records)["matches"]}
    return [rec for rec in records if str(rec.get("id") or "") in selected]


def match_connected_plugin(query: str, records: list[dict]) -> dict | None:
    """The connected connector whose name/type/alias equals `query`, if any."""
    q = (query or "").strip().lstrip("@")
    if not q:
        return None
    for rec in records:
        if not is_connected_record(rec):
            continue
        titles = mention_titles(str(rec.get("type") or ""), str(rec.get("name") or ""))
        if any(title.lower() == q.lower() for title in titles):
            return rec
    return None


def catalog() -> list[dict]:
    ranks = {t: i for i, t in enumerate(FEATURED)}
    out = []
    seen: set[str] = set()
    for c in CATALOG:
        type_ = str(c.get("type") or "")
        if not type_ or type_ in seen:
            continue
        seen.add(type_)
        entry = dict(c)
        entry["tools"] = runtime_tool_names(c["type"])
        entry["mcp"] = bool(c.get("mcp_url") or c.get("mcp_url_template"))
        # MCP types are usable once connected even without a static runtime.
        entry["implemented"] = bool(entry["tools"]) or entry["mcp"]
        rank = ranks.get(str(c.get("type") or ""))
        entry["featured"] = rank is not None
        if rank is not None:
            entry["featured_rank"] = rank
        extra = DOCS.get(str(c.get("type") or "")) or {}
        if extra.get("docs"):
            entry["docs"] = extra["docs"]
        if extra.get("notes"):
            entry["notes"] = extra["notes"]
        if extra.get("publisher"):
            entry["publisher"] = extra["publisher"]
        out.append(entry)
    return out


#: A tenant value substituted into `mcp_url_template` lands in the URL's host,
#: and that host is where the OAuth flow sends the user and the session later
#: sends their token. Keep it to a bare hostname — no scheme, no path, no
#: credentials, no query — so a mistyped or hostile field cannot redirect
#: either one somewhere else.
_HOST_VALUE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


def mcp_url(type_: str, config: Mapping[str, Any] | None = None) -> str | None:
    """The remote MCP server URL for a catalog type, if it has one.

    A static `mcp_url` serves every tenant. `mcp_url_template` is for the
    services that run one server per tenant — a Shopify store on its own
    domain — and is filled from that connector record's own config. It yields
    None while a field is missing or is not a bare hostname, so a half-built
    record falls back to the sign-in stub instead of aiming OAuth at a guess.
    """
    entry = _CATALOG_TYPES.get(type_) or {}
    url = entry.get("mcp_url")
    if url:
        return str(url)
    template = entry.get("mcp_url_template")
    if not template:
        return None
    values: dict[str, str] = {}
    for field in entry.get("fields", []):
        value = str((config or {}).get(field, "")).strip()
        if not _HOST_VALUE.match(value):
            return None
        values[field] = value
    try:
        return str(template).format(**values)
    except (KeyError, IndexError):  # template names a field the entry lacks
        return None


def account_name(type_: str, name: str | None, config: Mapping[str, Any] | None) -> str:
    """The record's display name: what the client sent, else the address
    for a per-mailbox type (`fields` has `email`) so two Gmail records read
    as their inboxes in the Accounts list and the tool namespace
    (`gmail_<address>_*`), else the catalog name."""
    entry = _CATALOG_TYPES.get(type_) or {}
    catalog_name = str(entry.get("name") or type_)
    given = str(name or "").strip()
    if given and given.lower() != catalog_name.lower():
        return given
    if "email" in (entry.get("fields") or []):
        address = str((config or {}).get("email") or "").strip()
        if address:
            return address
    return given or catalog_name


def _clean_enabled_for(value) -> list[str] | None:
    """Normalize an enabled_for payload: None = every bot, else bot names."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("enabled_for must be a list of bot names or null")
    return [str(v).strip() for v in value if str(v).strip()]


_LIST_CACHE: dict[str, tuple[tuple[int, int], list[dict]]] = {}

# One writer at a time per process: the post-connect tools refresh
# (`_refresh_mcp_tools`) rewrites the record from a thread while the API
# thread may be adding or granting, and each mutator is load → edit → save.
_WRITE_LOCK = threading.RLock()


def _locked(method):
    @functools.wraps(method)
    def inner(self, *args, **kwargs):
        with _WRITE_LOCK:
            return method(self, *args, **kwargs)

    return inner


@dataclass
class Connectors:
    paths: HarnessPaths

    @property
    def _file(self):
        return self.paths.home / "connectors.json"

    def _load(self) -> list[dict]:
        path = self._file
        key = str(path)
        if not path.is_file():
            _LIST_CACHE.pop(key, None)
            return []
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            return []
        hit = _LIST_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return list(hit[1])
        try:
            items = json.loads(path.read_text(encoding="utf-8")).get("connectors", [])
        except (json.JSONDecodeError, OSError):
            return []
        # tolerate hand-edited files: only dict records are connectors
        records = [i for i in items if isinstance(i, dict)]
        _LIST_CACHE[key] = (stamp, records)
        return list(records)

    def _save(self, items: list[dict]) -> None:
        """Replace the file atomically: a reader on another thread (the API
        listing connectors while the tools refresh saves) must never open a
        truncated, half-written document and conclude there are none."""
        self.paths.home.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(f"{self._file.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(
            json.dumps({"connectors": items}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._file)
        _LIST_CACHE.pop(str(self._file), None)

    def _public(self, item: dict) -> dict:
        from . import mcp_oauth

        out = dict(item)
        out.setdefault("enabled_for", None)
        out["secret_configured"] = (
            secret_source(f"connector_{item.get('id', '')}", self.paths) is not None
        )
        oauth = mcp_oauth.connected(self.paths, str(item.get("id", "")))
        out["oauth_configured"] = oauth
        type_ = str(item.get("type", ""))
        if type_ in WORKSPACE_TYPES:
            from . import delegated_oauth

            out["secret_configured"] = False
            out["oauth_configured"] = delegated_oauth.connected(self.paths, str(item.get("id", "")))
        cached = item.get("mcp_tools")
        prefer_static = bool(_CATALOG_TYPES.get(type_, {}).get("prefer_static"))
        if oauth and isinstance(cached, list) and cached and not prefer_static:
            out["tools"] = [str(t) for t in cached]
        else:
            out["tools"] = runtime_tool_names(type_)
        return out

    def list(self) -> list[dict]:
        return [self._public(item) for item in self._load()]

    def records(self) -> list[dict]:
        """Stored connector records, without UI-only enrichment."""
        return [dict(item) for item in self._load()]

    @_locked
    def add(
        self,
        type_: str,
        name: str,
        config: dict | None = None,
        secret: str | None = None,
        enabled_for: list[str] | None = None,
    ) -> dict:
        if type_ not in _CATALOG_TYPES:
            raise ValueError(f"unknown connector type {type_!r}")
        items = self._load()
        record = {
            "id": uuid.uuid4().hex[:8],
            "type": type_,
            "name": account_name(type_, name, config),
            "config": config or {},
            "auth": _CATALOG_TYPES[type_]["auth"],
            "status": "configured",
            "enabled_for": _clean_enabled_for(enabled_for),
            "created": time.time(),
        }
        items.append(record)
        self._save(items)
        if secret:
            set_secret(f"connector_{record['id']}", secret, self.paths)
            # Bots also probe conventional env names via get_secret / curl.
            if type_ == "github":
                set_secret("GITHUB_TOKEN", secret, self.paths)
        return self._public(record)

    @_locked
    def grant_bot(self, name: str, roster: list[str]) -> None:
        """Add a new bot to connectors that were granted to the whole fleet.

        `enabled_for is None` already means every bot. A list covering every
        other roster name is the UI's materialized "All bots" —
        without this, `create_bot` peers are silently excluded. A proper
        subset stays a subset. An empty list stays "no bots".
        """
        name = str(name or "").strip()
        if not name:
            return
        peers = {str(n).strip() for n in roster if str(n).strip() and n != name}
        if not peers:
            return
        items = self._load()
        changed = False
        for item in items:
            enabled = item.get("enabled_for")
            if not isinstance(enabled, list) or not enabled:
                continue
            if name in enabled:
                continue
            if not peers.issubset(set(enabled)):
                continue
            enabled.append(name)
            item["enabled_for"] = enabled
            changed = True
        if changed:
            self._save(items)

    @_locked
    def update(self, connector_id: str, *, name=_UNSET, config=_UNSET, enabled_for=_UNSET):
        """Update non-secret fields of a connector. Returns the record or None.

        `enabled_for` distinguishes null (all bots) from absent (unchanged),
        hence the sentinel defaults.
        """
        items = self._load()
        for item in items:
            if item["id"] != connector_id:
                continue
            if name is not _UNSET and str(name).strip():
                item["name"] = str(name).strip()
            if config is not _UNSET:
                item["config"] = dict(config or {})
            if enabled_for is not _UNSET:
                item["enabled_for"] = _clean_enabled_for(enabled_for)
            self._save(items)
            return self._public(item)
        return None

    @_locked
    def set_mcp_tools(self, connector_id: str, names: list[str]) -> None:
        """Cache the MCP server's tool names on the record (for the UI)."""
        items = self._load()
        for item in items:
            if item["id"] == connector_id:
                item["mcp_tools"] = [str(n) for n in names]
                self._save(items)
                return

    @_locked
    def remove(self, connector_id: str) -> bool:
        from . import mcp_oauth

        items = self._load()
        kept = [i for i in items if i["id"] != connector_id]
        if len(kept) == len(items):
            return False
        from . import delegated_oauth
        record = next(i for i in items if i["id"] == connector_id)
        if record["type"] in WORKSPACE_TYPES:
            delegated_oauth.disconnect(self.paths, record)
        self._save(kept)
        delete_secret(f"connector_{connector_id}", self.paths)
        mcp_oauth.clear_tokens(self.paths, connector_id)
        return True
