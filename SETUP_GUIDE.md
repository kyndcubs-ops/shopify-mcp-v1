# Setup Guide — From Pull Request to Fully Functional

A step-by-step walkthrough to get the Shopify MCP Server running and generating CRO-optimised websites through Claude.

---

## What you'll have when done

- A running MCP server connected to your Shopify store
- Claude able to manage your products, orders, customers, and inventory through natural language
- The ability to generate a complete, conversion-optimised Shopify website from just a store idea

---

## Prerequisites

Before you start, make sure you have:

| Requirement | Why |
|---|---|
| A **GitHub account** | To merge the PR and fork/deploy the repo |
| **Python 3.11+** installed locally | To run and test the server (check with `python --version`) |
| **pip** (Python package manager) | Comes with Python; used to install dependencies |
| A **Shopify store** (any plan) | The store you want to connect and manage |
| A **Claude.ai Pro, Team, or Enterprise** account | Required for remote MCP server connections |
| A **Railway account** (free tier works) | To deploy the server to a public URL (or any cloud host) |

---

## Step 1 — Merge the Pull Request

The website generation tools (`shopify_generate_website`, `shopify_apply_theme`, etc.) are in the PR branch. You need to merge them into `main`.

### On GitHub

1. Go to the repository: **https://github.com/kyndcubs-ops/shopify-mcp-v1**
2. Click the **Pull requests** tab
3. Open the PR titled **"Add shopify_generate_website and theme management MCP tools"** (or similar)
4. Review the changes — the PR adds 4 new tools:
   - `shopify_generate_website` — generates a full theme from a store idea
   - `shopify_list_themes` — lists themes on the store
   - `shopify_apply_theme` — pushes theme files to Shopify
   - `shopify_get_theme_asset` — reads a file from a theme
5. Click **Merge pull request** → **Confirm merge**
6. Optionally delete the branch after merging

### Verify the merge

After merging, the `main` branch should contain `server.py` with all 31 MCP tools (27 original + 4 new).

---

## Step 2 — Get Your Shopify Credentials

You need two things from Shopify:

### 2a. Find your store name

Your store name is the part **before** `.myshopify.com`.

Example:
- Admin URL: `https://acme-store.myshopify.com/admin`
- Store name: `acme-store`

> ❌ Don't use the full URL. Just the name part.

### 2b. Create a Custom App and get your access token

> ⚠️ A regular API key won't work. You need an **Admin API access token** from a Custom App.

1. Open **Shopify Admin** → **Settings** → **Apps and sales channels**
2. Click **Develop apps** (top right)
3. If prompted, click **Allow custom app development**
4. Click **Create an app** → name it `MCP Server` → click **Create app**
5. Go to the **Configuration** tab → click **Configure Admin API scopes**
6. Select **all** of these scopes:
   - `read_products`, `write_products`
   - `read_orders`, `write_orders`
   - `read_customers`, `write_customers`
   - `read_inventory`, `write_inventory`
   - `read_fulfillments`, `write_fulfillments`
   - `read_webhooks`, `write_webhooks`
   - `read_themes`, `write_themes` ← **required for website generation**
7. Click **Save**
8. Go to the **API credentials** tab → click **Install app** → confirm
9. Click **Reveal token once** and **copy it immediately** — it starts with `shpat_`

> 💡 Shopify only shows the token once. If you lose it, uninstall the app and reinstall to get a new one.

**Save these two values** — you'll need them in the next steps:
- Store name: e.g. `acme-store`
- Access token: e.g. `shpat_xxxxxxxxxxxxxxxxxxxxxxxx`

---

## Step 3 — Set Up the Server Locally (Test First)

Before deploying to the cloud, run the server on your machine to make sure everything works.

### 3a. Clone the repository

```bash
git clone https://github.com/kyndcubs-ops/shopify-mcp-v1.git
cd shopify-mcp-v1
```

### 3b. Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `mcp[cli]` — the FastMCP server framework
- `httpx` — HTTP client for Shopify API calls
- `pydantic` — input validation for all tools
- `uvicorn` — the ASGI server

### 3c. Create your environment file

```bash
cp env.example .env
```

Open `.env` in a text editor and fill in your values:

```env
SHOPIFY_STORE=acme-store
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxxxxxx
```

> Leave everything else at the defaults. Only these two are required.

### 3d. Start the server

```bash
python server.py
```

You should see:

```
2026-04-08 14:00:00,000 INFO Token mode: static SHOPIFY_ACCESS_TOKEN (no auto-refresh)
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 3e. Verify it works

Open a new terminal and test with curl:

```bash
# Check if the server responds
curl http://localhost:8000/mcp
```

You should get a response (not a connection error or 404).

> Press `Ctrl+C` to stop the server when you're done testing.

---

## Step 4 — Deploy to the Cloud

Claude.ai needs a **public URL** to connect to your server. The easiest way is Railway (free tier).

### 4a. Fork the repo

1. Go to **https://github.com/kyndcubs-ops/shopify-mcp-v1**
2. Click **Fork** (top right) → fork to your own GitHub account

### 4b. Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your forked `shopify-mcp-v1` repo
4. Railway detects the `Dockerfile` and starts building automatically
5. Wait for the build to complete (1-2 minutes)

### 4c. Generate a public domain

1. In Railway, go to your deployed service → **Settings** → **Networking**
2. Click **Generate Domain**
3. Copy your URL — it looks like: `https://shopify-mcp-v1-production.up.railway.app`

### 4d. Add environment variables

In Railway, go to your service → **Variables** and add:

| Variable | Value |
|---|---|
| `SHOPIFY_STORE` | `acme-store` (your store name) |
| `SHOPIFY_ACCESS_TOKEN` | `shpat_xxxxxxxxxxxxxxxxxxxxxxxx` (your token) |
| `PORT` | `8000` |
| `MCP_TRANSPORT` | `streamable-http` |

Railway will automatically restart with the new variables.

### 4e. Verify deployment

Open your browser and visit:

```
https://your-railway-domain.up.railway.app/mcp
```

You should get a response (not a 404 or error page). If so, your server is live.

---

## Step 5 — Connect to Claude

### 5a. Get your MCP endpoint URL

Your endpoint is your Railway URL + `/mcp`:

```
https://your-railway-domain.up.railway.app/mcp
```

### 5b. Add the integration in Claude.ai

1. Go to [claude.ai](https://claude.ai)
2. Click your **profile icon** (bottom left) → **Settings**
3. Go to **Integrations**
4. Click **Add integration**
5. Fill in:
   - **Name:** `Shopify`
   - **URL:** `https://your-railway-domain.up.railway.app/mcp`
   - **Authentication token:** leave blank (or set one — see step 5c)
6. Click **Save**

### 5c. (Recommended) Secure with a bearer token

Without a token, anyone who knows your Railway URL can access your Shopify data.

**In Railway Variables, add:**

```
BEARER_TOKEN=pick-a-long-random-string-here
```

For example: `BEARER_TOKEN=mY-s3cRet-T0k3n-2026-xyzABC`

**In Claude.ai**, go to your Shopify integration settings and paste the same token into the **authentication token** field.

> The `server.py` README has instructions for adding auth middleware if it's not already in the code. Check the README section "Securing your server with a bearer token".

---

## Step 6 — Verify Everything Works

Open a new chat in Claude.ai and test each capability:

### Test 1: Store connection

Type:
```
Get my Shopify store info
```

Expected: Claude calls `shopify_get_shop` and returns your store name, currency, plan, domain, etc.

### Test 2: Products

Type:
```
How many active products do I have?
```

Expected: Claude calls `shopify_count_products` and returns a number.

### Test 3: Orders

Type:
```
Show me the latest 5 orders
```

Expected: Claude calls `shopify_list_orders` and shows order details.

### Test 4: Website generation

Type:
```
Generate a Shopify website for a kids toy store called "Kynd Cubs" targeting Indian parents, 
INR currency, free shipping above ₹499, WhatsApp +917509802310
```

Expected: Claude calls `shopify_generate_website` and returns a full theme configuration with all files and CRO features.

### Test 5: Apply the theme

Type:
```
List my Shopify themes
```

Then:
```
Apply the generated theme to my active theme (ID: xxxxx)
```

Expected: Claude calls `shopify_list_themes` to find the active theme, then `shopify_apply_theme` to push the files.

---

## Step 7 — Generate Your Website (Full Workflow)

This is the complete flow to create a CRO-optimised Shopify website from scratch:

### 7a. Tell Claude about your store

In a Claude chat, say something like:

```
I want to create a Shopify website. Here's my store info:

- Store name: Kynd Cubs
- Idea: Curated educational toys for Indian kids aged 1-8
- Niche: kids
- Target audience: Indian parents of toddlers
- Currency: INR
- Country: India
- Free shipping above: ₹499
- WhatsApp: +917509802310
- USPs: Safety tested, Pan-India delivery, Easy returns
- Brand story: We got tired of toys abandoned in 3 days. We built Kynd Cubs 
  to fix that — handpicking only the toys that genuinely hold a child's attention.
- Tagline: Handpicked by parents. Tested by real Indian families.
- Hero headline: Toys your child [em]won't ignore[/em]
- Main collection: frontpage

Generate the website.
```

### 7b. Review the output

Claude will return a JSON blueprint containing:

- **13 theme files** — index.json, product.json, cart.json, collection.json, 404.json, etc.
- **Niche-matched colour palette** — auto-selected based on your niche
- **CRO features** — 13 conversion-optimisation features listed
- **Setup instructions** — next steps to complete

### 7c. Apply to your store

Tell Claude:

```
List my Shopify themes and apply this to the active theme.
```

Claude will:
1. Call `shopify_list_themes` to find your active theme ID
2. Call `shopify_apply_theme` to upload all 13 files to your theme

### 7d. Customise in Shopify

After applying:

1. Go to **Shopify Admin** → **Online Store** → **Themes** → **Customize**
2. Upload images for each section (hero, brand story, UGC gallery, etc.)
3. Adjust text, colours, and links as needed
4. Preview your store and publish when ready

---

## What Each Section Does

| Section | Purpose | CRO Feature |
|---|---|---|
| **Announcement Bar** | Rotating messages at the top | Free shipping threshold + social proof |
| **Hero Banner** | Full-width banner with headline | Primary CTA + secondary CTA + proof strip |
| **Best Sellers** | Product carousel | Quick add-to-cart (no page reload) |
| **Brand Story** | About section with stats | Emotional connection + trust building |
| **UGC Gallery** | Customer photos/videos | Social proof from real customers |
| **Trust Strip** | Badge row | Safety, delivery, returns, COD trust signals |
| **Email Signup** | Newsletter form | First-order discount incentive |
| **Product Gallery** | Image carousel on PDP | Zoom + swipe + video support |
| **Product Info** | Title, price, badges on PDP | Rotating review hooks + urgency badges + sold count |
| **Action Zone** | Variant selector + ATC | One-click buy + quantity selector |
| **Offers Bar** | Discount banners on PDP | Bundle/discount visibility |
| **Product Tabs** | Description/specs tabs | Organised info reduces bounce |
| **Reviews** | Review widget | Star ratings + review count |
| **FAQ Accordion** | Expandable Q&A | Reduces purchase hesitation |
| **Policy Grid** | Shipping/returns/warranty | Transparency builds trust |
| **Sticky ATC** | Fixed add-to-cart bar | Always-visible purchase button |
| **Floating WhatsApp** | Chat button | Instant support reduces abandonment |

---

## File Structure Reference

```
shopify-mcp-v1/
├── server.py              ← MCP server with all 31 tools
├── requirements.txt       ← Python dependencies
├── Dockerfile             ← Container config for Railway/Docker
├── env.example            ← Template for environment variables
├── README.md              ← Project documentation
├── SETUP_GUIDE.md         ← This file
├── assets/
│   └── kyndcubs-global.css  ← Global CSS (design system, utilities, layout)
├── config/
│   ├── settings_data.json   ← Theme colour palette and fonts
│   └── settings_schema.json ← Theme editor settings definition
├── layout/
│   ├── theme.liquid         ← Main HTML wrapper for all pages
│   └── password.liquid      ← Password page layout
├── sections/               ← All Liquid section templates
│   ├── header-group.json    ← Header section group
│   ├── footer-group.json    ← Footer section group
│   ├── kc-*.liquid          ← Homepage sections (v2)
│   └── kcp-*.liquid         ← Product page sections (v4)
└── templates/              ← Page template configurations
    ├── index.json           ← Homepage (sections + order)
    ├── product.kyndcubs.json ← Product page (sections + order)
    ├── cart.json            ← Cart page
    ├── collection.json      ← Collection page
    ├── 404.json             ← 404 page
    ├── article.json         ← Blog article
    ├── blog.json            ← Blog listing
    ├── page.json            ← Generic page
    ├── password.json        ← Password page
    ├── search.json          ← Search results
    ├── gift_card.liquid     ← Gift card template
    └── customers/           ← Customer account pages
        ├── account.json
        ├── addresses.json
        ├── login.json
        ├── order.json
        └── register.json
```

---

## All Available MCP Tools (31 total)

### Products (6)
| Tool | What it does |
|---|---|
| `shopify_list_products` | List products with filters (status, type, vendor) |
| `shopify_get_product` | Get full product details by ID |
| `shopify_create_product` | Create a new product with variants and images |
| `shopify_update_product` | Update product fields |
| `shopify_delete_product` | Permanently delete a product |
| `shopify_count_products` | Count products with optional filters |

### Orders (4)
| Tool | What it does |
|---|---|
| `shopify_list_orders` | List orders with status/date/payment filters |
| `shopify_get_order` | Get full order details by ID |
| `shopify_count_orders` | Count orders |
| `shopify_close_order` | Close (complete) an order |
| `shopify_cancel_order` | Cancel an order with optional restock/email |

### Customers (6)
| Tool | What it does |
|---|---|
| `shopify_list_customers` | List all customers |
| `shopify_search_customers` | Search by name or email |
| `shopify_get_customer` | Get customer details by ID |
| `shopify_create_customer` | Create a new customer |
| `shopify_update_customer` | Update customer fields |
| `shopify_get_customer_orders` | Get all orders for a customer |

### Collections (2)
| Tool | What it does |
|---|---|
| `shopify_list_collections` | List custom or smart collections |
| `shopify_get_collection_products` | Get products in a collection |

### Inventory (3)
| Tool | What it does |
|---|---|
| `shopify_list_locations` | List inventory locations |
| `shopify_get_inventory_levels` | Get stock levels by location/item |
| `shopify_set_inventory_level` | Set available quantity |

### Fulfillments (2)
| Tool | What it does |
|---|---|
| `shopify_list_fulfillments` | List shipments for an order |
| `shopify_create_fulfillment` | Ship an order with tracking info |

### Shop & Webhooks (3)
| Tool | What it does |
|---|---|
| `shopify_get_shop` | Get store info (name, currency, plan) |
| `shopify_list_webhooks` | List configured webhooks |
| `shopify_create_webhook` | Register a new webhook |

### Website Generation & Themes (4)
| Tool | What it does |
|---|---|
| `shopify_generate_website` | Generate a full CRO theme from a store idea |
| `shopify_list_themes` | List all themes on the store |
| `shopify_apply_theme` | Push theme files to a Shopify theme |
| `shopify_get_theme_asset` | Read a single file from a theme |

---

## Troubleshooting

### "Authentication failed" (401)
Your `SHOPIFY_ACCESS_TOKEN` is wrong or expired. Make sure it starts with `shpat_` and that the Custom App is installed.

### "Permission denied" (403)
Your token is missing API scopes. Go to your Custom App → Configuration → add missing scopes → Save → reinstall the app (generates new token).

### "Missing SHOPIFY_STORE environment variable"
`SHOPIFY_STORE` should be just the store name: ✅ `acme-store` ❌ `acme-store.myshopify.com`

### Claude can't connect
- Check that Railway deployment is active and has a domain generated
- Test by visiting `https://your-domain.up.railway.app/mcp` in a browser
- Make sure the bearer token matches between Railway and Claude (if using one)

### Theme files not updating
- Make sure you have `read_themes` and `write_themes` scopes on your Shopify token
- Use `shopify_list_themes` to verify you're using the correct theme ID
- Check that the theme ID points to the **active** (published) theme

### Lost your Shopify token
Shopify only shows it once. Go to Shopify Admin → Settings → Apps → your app → API credentials → Uninstall app → Install app → Reveal token once.

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────────┐
│                    QUICK START                              │
│                                                             │
│  1. Merge the PR on GitHub                                  │
│  2. Get Shopify token (shpat_...)                           │
│  3. Fork → Deploy to Railway                                │
│  4. Set env vars: SHOPIFY_STORE + SHOPIFY_ACCESS_TOKEN      │
│  5. Add MCP integration in Claude.ai                        │
│  6. Chat: "Generate a Shopify website for [your idea]"      │
│  7. Chat: "Apply it to my active theme"                     │
│  8. Customise images in Shopify Theme Editor                │
│                                                             │
│  Server URL: https://your-app.up.railway.app/mcp            │
│  Local test: python server.py → http://localhost:8000/mcp   │
│  Build:      pip install -r requirements.txt                │
└─────────────────────────────────────────────────────────────┘
```
