#!/usr/bin/env python3
"""
Shopify MCP Server — Full Admin API access via FastMCP.
Provides tools for managing products, orders, customers, collections,
inventory, and fulfillments through the Shopify Admin REST API.

Token Management:
  - Uses client_credentials grant to auto-generate and refresh tokens
  - Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (recommended for OAuth apps)
  - Falls back to static SHOPIFY_ACCESS_TOKEN if client credentials not set
"""
import json
import os
import logging
import time
import asyncio
from typing import Optional, List, Dict, Any
from enum import Enum
import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOPIFY_STORE        = os.environ.get("SHOPIFY_STORE", "")           # e.g. "my-store"
SHOPIFY_TOKEN        = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")    # Static token (shpat_...)
SHOPIFY_CLIENT_ID    = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
API_VERSION          = os.environ.get("SHOPIFY_API_VERSION", "2024-10")

# Refresh buffer: refresh token 30 minutes before expiry (only used with OAuth)
TOKEN_REFRESH_BUFFER = int(os.environ.get("TOKEN_REFRESH_BUFFER", "1800"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shopify_mcp")

PORT          = int(os.environ.get("PORT", "8000"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")

mcp = FastMCP("shopify_mcp", host="0.0.0.0", port=PORT, json_response=True)


# ---------------------------------------------------------------------------
# Token Manager — handles automatic token lifecycle
# ---------------------------------------------------------------------------

class TokenManager:
    """
    Manages Shopify Admin API access tokens.

    Two modes:
      1. Static token  — set SHOPIFY_ACCESS_TOKEN (recommended for Custom Apps)
      2. OAuth / client_credentials — set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
         Enables auto-refresh before expiry and retry on 401.
    """

    def __init__(
        self,
        store: str,
        client_id: str,
        client_secret: str,
        static_token: str = "",
        refresh_buffer: int = 1800,
    ):
        self._store         = store
        self._client_id     = client_id
        self._client_secret = client_secret
        self._static_token  = static_token
        self._refresh_buffer = refresh_buffer

        self._access_token: str   = ""
        self._expires_at: float   = 0.0
        self._lock = asyncio.Lock()

        self._use_client_credentials = bool(client_id and client_secret)

        if self._use_client_credentials:
            logger.info("Token mode: client_credentials (auto-refresh enabled)")
        elif static_token:
            logger.info("Token mode: static SHOPIFY_ACCESS_TOKEN (no auto-refresh)")
            self._access_token = static_token
            self._expires_at   = float("inf")
        else:
            logger.warning(
                "No credentials configured. Set SHOPIFY_ACCESS_TOKEN or "
                "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET."
            )

    @property
    def is_expired(self) -> bool:
        if not self._access_token:
            return True
        return time.time() >= (self._expires_at - self._refresh_buffer)

    async def get_token(self) -> str:
        if not self.is_expired:
            return self._access_token

        async with self._lock:
            if not self.is_expired:
                return self._access_token

            if self._use_client_credentials:
                await self._refresh_token()
            elif not self._access_token:
                raise RuntimeError(
                    "No valid token available. "
                    "Set SHOPIFY_ACCESS_TOKEN in your environment variables."
                )

        return self._access_token

    async def force_refresh(self) -> str:
        if not self._use_client_credentials:
            raise RuntimeError(
                "Cannot refresh — using a static token. "
                "Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET to enable auto-refresh."
            )
        async with self._lock:
            await self._refresh_token()
        return self._access_token

    async def _refresh_token(self) -> None:
        url = f"https://{self._store}.myshopify.com/admin/oauth/access_token"
        logger.info("Refreshing Shopify access token via client_credentials grant...")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

            if resp.status_code != 200:
                logger.error(f"Token refresh failed ({resp.status_code}): {resp.text[:500]}")
                raise RuntimeError(
                    f"Token refresh failed ({resp.status_code}). "
                    "Check SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET."
                )

            data               = resp.json()
            self._access_token = data["access_token"]
            expires_in         = data.get("expires_in", 86399)
            self._expires_at   = time.time() + expires_in

            scope         = data.get("scope", "")
            scope_preview = scope[:80] + "..." if len(scope) > 80 else scope
            logger.info(
                f"Token refreshed. Expires in {expires_in}s "
                f"({expires_in // 3600}h {(expires_in % 3600) // 60}m). "
                f"Scopes: {scope_preview}"
            )


# Global token manager
token_manager = TokenManager(
    store=SHOPIFY_STORE,
    client_id=SHOPIFY_CLIENT_ID,
    client_secret=SHOPIFY_CLIENT_SECRET,
    static_token=SHOPIFY_TOKEN,
    refresh_buffer=TOKEN_REFRESH_BUFFER,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}"


async def _headers() -> dict:
    token = await token_manager.get_token()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    body:   Optional[dict] = None,
    _retried: bool = False,
) -> dict:
    """Central HTTP helper — every API call flows through here.
    Auto-retries once on 401 when using OAuth credentials.
    """
    if not SHOPIFY_STORE:
        raise RuntimeError(
            "Missing SHOPIFY_STORE environment variable. "
            "Set it before starting the server."
        )

    url     = f"{_base_url()}/{path}"
    headers = await _headers()

    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method, url,
            headers=headers,
            params=params,
            json=body,
            timeout=30.0,
        )

        if resp.status_code == 401 and not _retried and token_manager._use_client_credentials:
            logger.warning("Got 401 — refreshing token and retrying...")
            await token_manager.force_refresh()
            return await _request(method, path, params=params, body=body, _retried=True)

        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()


def _error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        messages = {
            401: "Authentication failed — check your SHOPIFY_ACCESS_TOKEN (should start with shpat_).",
            403: "Permission denied — your token may be missing required API scopes.",
            404: "Resource not found — double-check the ID.",
            422: f"Validation error: {json.dumps(detail)}",
            429: "Rate-limited — wait a moment and retry.",
        }
        return messages.get(status, f"Shopify API error {status}: {json.dumps(detail)}")
    if isinstance(e, httpx.TimeoutException):
        return "Request timed out — try again."
    if isinstance(e, RuntimeError):
        return str(e)
    return f"Unexpected error: {type(e).__name__}: {e}"


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ═══════════════════════════════════════════════════════════════════════════

class ListProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int]  = Field(default=50, ge=1, le=250, description="Max products to return (1-250)")
    status:         Optional[str]  = Field(default=None, description="Filter by status: active, archived, draft")
    product_type:   Optional[str]  = Field(default=None, description="Filter by product type")
    vendor:         Optional[str]  = Field(default=None, description="Filter by vendor name")
    collection_id:  Optional[int]  = Field(default=None, description="Filter by collection ID")
    since_id:       Optional[int]  = Field(default=None, description="Pagination: return products after this ID")
    fields:         Optional[str]  = Field(default=None, description="Comma-separated fields to include")


@mcp.tool(
    name="shopify_list_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_products(params: ListProductsInput) -> str:
    """List products from the Shopify store with optional filters."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for field in ["status", "product_type", "vendor", "collection_id", "since_id", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


class GetProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="The Shopify product ID")


@mcp.tool(
    name="shopify_get_product",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_product(params: GetProductInput) -> str:
    """Retrieve a single product by ID, including all variants and images."""
    try:
        data = await _request("GET", f"products/{params.product_id}.json")
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class CreateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title:        str                        = Field(..., min_length=1, description="Product title")
    body_html:    Optional[str]              = Field(default=None, description="HTML description")
    vendor:       Optional[str]              = Field(default=None)
    product_type: Optional[str]              = Field(default=None)
    tags:         Optional[str]              = Field(default=None, description="Comma-separated tags")
    status:       Optional[str]              = Field(default="draft", description="active, archived, or draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None, description="Variant objects with price, sku, etc.")
    options:      Optional[List[Dict[str, Any]]] = Field(default=None, description="Product options (Size, Color, etc.)")
    images:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Image objects with src URL")


@mcp.tool(
    name="shopify_create_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_product(params: CreateProductInput) -> str:
    """Create a new product in the Shopify store."""
    try:
        product: Dict[str, Any] = {"title": params.title}
        for field in ["body_html", "vendor", "product_type", "tags", "status", "variants", "options", "images"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("POST", "products.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class UpdateProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id:   int            = Field(..., description="Product ID to update")
    title:        Optional[str]  = Field(default=None)
    body_html:    Optional[str]  = Field(default=None)
    vendor:       Optional[str]  = Field(default=None)
    product_type: Optional[str]  = Field(default=None)
    tags:         Optional[str]  = Field(default=None)
    status:       Optional[str]  = Field(default=None, description="active, archived, or draft")
    variants:     Optional[List[Dict[str, Any]]] = Field(default=None)


@mcp.tool(
    name="shopify_update_product",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_product(params: UpdateProductInput) -> str:
    """Update an existing product. Only provided fields are changed."""
    try:
        product: Dict[str, Any] = {}
        for field in ["title", "body_html", "vendor", "product_type", "tags", "status", "variants"]:
            val = getattr(params, field)
            if val is not None:
                product[field] = val
        data = await _request("PUT", f"products/{params.product_id}.json", body={"product": product})
        return _fmt(data.get("product", data))
    except Exception as e:
        return _error(e)


class DeleteProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., description="Product ID to delete")


@mcp.tool(
    name="shopify_delete_product",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_delete_product(params: DeleteProductInput) -> str:
    """Permanently delete a product. This cannot be undone."""
    try:
        await _request("DELETE", f"products/{params.product_id}.json")
        return f"Product {params.product_id} deleted."
    except Exception as e:
        return _error(e)


class ProductCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:       Optional[str] = Field(default=None, description="active, archived, or draft")
    vendor:       Optional[str] = Field(default=None)
    product_type: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_products(params: ProductCountInput) -> str:
    """Get the total count of products, optionally filtered."""
    try:
        p: Dict[str, Any] = {}
        for field in ["status", "vendor", "product_type"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "products/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════════════════

class ListOrdersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:               Optional[int] = Field(default=50, ge=1, le=250)
    status:              Optional[str] = Field(default="any", description="open, closed, cancelled, any")
    financial_status:    Optional[str] = Field(default=None, description="authorized, pending, paid, refunded, voided, any")
    fulfillment_status:  Optional[str] = Field(default=None, description="shipped, partial, unshipped, unfulfilled, any")
    since_id:            Optional[int] = Field(default=None)
    created_at_min:      Optional[str] = Field(default=None, description="ISO 8601 date, e.g. 2024-01-01T00:00:00Z")
    created_at_max:      Optional[str] = Field(default=None)
    fields:              Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_orders(params: ListOrdersInput) -> str:
    """List orders with optional filters for status, financial/fulfillment status, and date range."""
    try:
        p: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        for field in ["financial_status", "fulfillment_status", "since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data   = await _request("GET", "orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


class GetOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="The Shopify order ID")


@mcp.tool(
    name="shopify_get_order",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_order(params: GetOrderInput) -> str:
    """Retrieve a single order by ID with full details."""
    try:
        data = await _request("GET", f"orders/{params.order_id}.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class OrderCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status:             Optional[str] = Field(default="any")
    financial_status:   Optional[str] = Field(default=None)
    fulfillment_status: Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_count_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_count_orders(params: OrderCountInput) -> str:
    """Get total order count, optionally filtered."""
    try:
        p: Dict[str, Any] = {"status": params.status}
        for field in ["financial_status", "fulfillment_status"]:
            val = getattr(params, field)
            if val is not None:
                p[field] = val
        data = await _request("GET", "orders/count.json", params=p)
        return _fmt(data)
    except Exception as e:
        return _error(e)


class CloseOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(..., description="Order ID to close")


@mcp.tool(
    name="shopify_close_order",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_close_order(params: CloseOrderInput) -> str:
    """Close an order (marks it as completed)."""
    try:
        data = await _request("POST", f"orders/{params.order_id}/close.json")
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


class CancelOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int            = Field(..., description="Order ID to cancel")
    reason:   Optional[str]  = Field(default=None, description="customer, fraud, inventory, declined, other")
    email:    Optional[bool] = Field(default=True,  description="Send cancellation email to customer")
    restock:  Optional[bool] = Field(default=False, description="Restock line items")


@mcp.tool(
    name="shopify_cancel_order",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_cancel_order(params: CancelOrderInput) -> str:
    """Cancel an order. Optionally restock items and notify the customer."""
    try:
        body: Dict[str, Any] = {}
        for field in ["reason", "email", "restock"]:
            val = getattr(params, field)
            if val is not None:
                body[field] = val
        data = await _request("POST", f"orders/{params.order_id}/cancel.json", body=body)
        return _fmt(data.get("order", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════════════════

class ListCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit:          Optional[int] = Field(default=50, ge=1, le=250)
    since_id:       Optional[int] = Field(default=None)
    created_at_min: Optional[str] = Field(default=None, description="ISO 8601 date")
    created_at_max: Optional[str] = Field(default=None)
    fields:         Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_list_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_customers(params: ListCustomersInput) -> str:
    """List customers from the store."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        for f in ["since_id", "created_at_min", "created_at_max", "fields"]:
            val = getattr(params, f)
            if val is not None:
                p[f] = val
        data      = await _request("GET", "customers.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class SearchCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str           = Field(..., min_length=1, description="Search query (name, email, etc.)")
    limit: Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_search_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_search_customers(params: SearchCustomersInput) -> str:
    """Search customers by name, email, or other fields."""
    try:
        p         = {"query": params.query, "limit": params.limit}
        data      = await _request("GET", "customers/search.json", params=p)
        customers = data.get("customers", [])
        return _fmt({"count": len(customers), "customers": customers})
    except Exception as e:
        return _error(e)


class GetCustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int = Field(..., description="Shopify customer ID")


@mcp.tool(
    name="shopify_get_customer",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer(params: GetCustomerInput) -> str:
    """Retrieve a single customer by ID."""
    try:
        data = await _request("GET", f"customers/{params.customer_id}.json")
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CreateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    first_name:         Optional[str]  = Field(default=None)
    last_name:          Optional[str]  = Field(default=None)
    email:              Optional[str]  = Field(default=None)
    phone:              Optional[str]  = Field(default=None)
    tags:               Optional[str]  = Field(default=None)
    note:               Optional[str]  = Field(default=None)
    addresses:          Optional[List[Dict[str, Any]]] = Field(default=None)
    send_email_invite:  Optional[bool] = Field(default=False)


@mcp.tool(
    name="shopify_create_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_customer(params: CreateCustomerInput) -> str:
    """Create a new customer."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note", "addresses", "send_email_invite"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("POST", "customers.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class UpdateCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    customer_id: int           = Field(..., description="Customer ID to update")
    first_name:  Optional[str] = Field(default=None)
    last_name:   Optional[str] = Field(default=None)
    email:       Optional[str] = Field(default=None)
    phone:       Optional[str] = Field(default=None)
    tags:        Optional[str] = Field(default=None)
    note:        Optional[str] = Field(default=None)


@mcp.tool(
    name="shopify_update_customer",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_update_customer(params: UpdateCustomerInput) -> str:
    """Update an existing customer. Only provided fields are changed."""
    try:
        customer: Dict[str, Any] = {}
        for field in ["first_name", "last_name", "email", "phone", "tags", "note"]:
            val = getattr(params, field)
            if val is not None:
                customer[field] = val
        data = await _request("PUT", f"customers/{params.customer_id}.json", body={"customer": customer})
        return _fmt(data.get("customer", data))
    except Exception as e:
        return _error(e)


class CustomerOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int           = Field(..., description="Customer ID")
    limit:       Optional[int] = Field(default=50, ge=1, le=250)
    status:      Optional[str] = Field(default="any")


@mcp.tool(
    name="shopify_get_customer_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_customer_orders(params: CustomerOrdersInput) -> str:
    """Get all orders for a specific customer."""
    try:
        p      = {"limit": params.limit, "status": params.status}
        data   = await _request("GET", f"customers/{params.customer_id}/orders.json", params=p)
        orders = data.get("orders", [])
        return _fmt({"count": len(orders), "orders": orders})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# COLLECTIONS (Custom + Smart)
# ═══════════════════════════════════════════════════════════════════════════

class ListCollectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit:           Optional[int] = Field(default=50, ge=1, le=250)
    since_id:        Optional[int] = Field(default=None)
    collection_type: Optional[str] = Field(default="custom", description="'custom' or 'smart'")


@mcp.tool(
    name="shopify_list_collections",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_collections(params: ListCollectionsInput) -> str:
    """List custom or smart collections."""
    try:
        endpoint = "custom_collections.json" if params.collection_type == "custom" else "smart_collections.json"
        p: Dict[str, Any] = {"limit": params.limit}
        if params.since_id:
            p["since_id"] = params.since_id
        data = await _request("GET", endpoint, params=p)
        key  = "custom_collections" if params.collection_type == "custom" else "smart_collections"
        collections = data.get(key, [])
        return _fmt({"count": len(collections), "collections": collections})
    except Exception as e:
        return _error(e)


class GetCollectionProductsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_id: int           = Field(..., description="Collection ID")
    limit:         Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_get_collection_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_collection_products(params: GetCollectionProductsInput) -> str:
    """Get all products in a specific collection."""
    try:
        p        = {"limit": params.limit, "collection_id": params.collection_id}
        data     = await _request("GET", "products.json", params=p)
        products = data.get("products", [])
        return _fmt({"count": len(products), "products": products})
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# INVENTORY
# ═══════════════════════════════════════════════════════════════════════════

class ListInventoryLocationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_list_locations",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_locations(params: ListInventoryLocationsInput) -> str:
    """List all inventory locations for the store."""
    try:
        data      = await _request("GET", "locations.json")
        locations = data.get("locations", [])
        return _fmt({"count": len(locations), "locations": locations})
    except Exception as e:
        return _error(e)


class GetInventoryLevelsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id:         Optional[int] = Field(default=None, description="Filter by location ID")
    inventory_item_ids:  Optional[str] = Field(default=None, description="Comma-separated inventory item IDs")


@mcp.tool(
    name="shopify_get_inventory_levels",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_inventory_levels(params: GetInventoryLevelsInput) -> str:
    """Get inventory levels for specific locations or inventory items."""
    try:
        p: Dict[str, Any] = {}
        if params.location_id:
            p["location_ids"] = params.location_id
        if params.inventory_item_ids:
            p["inventory_item_ids"] = params.inventory_item_ids
        data   = await _request("GET", "inventory_levels.json", params=p)
        levels = data.get("inventory_levels", [])
        return _fmt({"count": len(levels), "inventory_levels": levels})
    except Exception as e:
        return _error(e)


class SetInventoryLevelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_item_id: int = Field(..., description="Inventory item ID")
    location_id:       int = Field(..., description="Location ID")
    available:         int = Field(..., description="Available quantity to set")


@mcp.tool(
    name="shopify_set_inventory_level",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_set_inventory_level(params: SetInventoryLevelInput) -> str:
    """Set the available inventory for an item at a location."""
    try:
        body = {
            "inventory_item_id": params.inventory_item_id,
            "location_id":       params.location_id,
            "available":         params.available,
        }
        data = await _request("POST", "inventory_levels/set.json", body=body)
        return _fmt(data.get("inventory_level", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# FULFILLMENTS
# ═══════════════════════════════════════════════════════════════════════════

class ListFulfillmentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int           = Field(..., description="Order ID")
    limit:    Optional[int] = Field(default=50, ge=1, le=250)


@mcp.tool(
    name="shopify_list_fulfillments",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_fulfillments(params: ListFulfillmentsInput) -> str:
    """List fulfillments for a specific order."""
    try:
        p            = {"limit": params.limit}
        data         = await _request("GET", f"orders/{params.order_id}/fulfillments.json", params=p)
        fulfillments = data.get("fulfillments", [])
        return _fmt({"count": len(fulfillments), "fulfillments": fulfillments})
    except Exception as e:
        return _error(e)


class CreateFulfillmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id:         int                        = Field(..., description="Order ID to fulfill")
    location_id:      int                        = Field(..., description="Location ID fulfilling from")
    tracking_number:  Optional[str]              = Field(default=None)
    tracking_company: Optional[str]              = Field(default=None, description="e.g. UPS, FedEx, USPS")
    tracking_url:     Optional[str]              = Field(default=None)
    line_items:       Optional[List[Dict[str, Any]]] = Field(default=None, description="Specific line items (omit for all)")
    notify_customer:  Optional[bool]             = Field(default=True, description="Send shipping notification email")


@mcp.tool(
    name="shopify_create_fulfillment",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_fulfillment(params: CreateFulfillmentInput) -> str:
    """Create a fulfillment for an order (ship items)."""
    try:
        fulfillment: Dict[str, Any] = {"location_id": params.location_id}
        for field in ["tracking_number", "tracking_company", "tracking_url", "line_items", "notify_customer"]:
            val = getattr(params, field)
            if val is not None:
                fulfillment[field] = val
        data = await _request(
            "POST",
            f"orders/{params.order_id}/fulfillments.json",
            body={"fulfillment": fulfillment},
        )
        return _fmt(data.get("fulfillment", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# SHOP INFO
# ═══════════════════════════════════════════════════════════════════════════

class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_get_shop",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_get_shop(params: EmptyInput) -> str:
    """Get store information: name, domain, plan, currency, timezone, etc."""
    try:
        data = await _request("GET", "shop.json")
        return _fmt(data.get("shop", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════

class ListWebhooksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: Optional[int] = Field(default=50, ge=1, le=250)
    topic: Optional[str] = Field(default=None, description="Filter by topic, e.g. orders/create")


@mcp.tool(
    name="shopify_list_webhooks",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def shopify_list_webhooks(params: ListWebhooksInput) -> str:
    """List configured webhooks."""
    try:
        p: Dict[str, Any] = {"limit": params.limit}
        if params.topic:
            p["topic"] = params.topic
        data     = await _request("GET", "webhooks.json", params=p)
        webhooks = data.get("webhooks", [])
        return _fmt({"count": len(webhooks), "webhooks": webhooks})
    except Exception as e:
        return _error(e)


class CreateWebhookInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    topic:   str           = Field(..., description="Webhook topic, e.g. orders/create, products/update")
    address: str           = Field(..., description="URL to receive the webhook POST")
    format:  Optional[str] = Field(default="json", description="json or xml")


@mcp.tool(
    name="shopify_create_webhook",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def shopify_create_webhook(params: CreateWebhookInput) -> str:
    """Create a new webhook subscription."""
    try:
        webhook = {"topic": params.topic, "address": params.address, "format": params.format}
        data    = await _request("POST", "webhooks.json", body={"webhook": webhook})
        return _fmt(data.get("webhook", data))
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# WEBSITE GENERATION — CRO-OPTIMIZED THEME FROM STORE IDEA
# ═══════════════════════════════════════════════════════════════════════════

# --- colour-palette presets by niche -----------------------------------
_NICHE_PALETTES: Dict[str, Dict[str, str]] = {
    "kids": {
        "color_primary": "#6B5FD4", "color_primary_bg": "#F0EDFF",
        "color_page_bg": "#FBF8F4", "color_ink": "#2C2A3A",
    },
    "fashion": {
        "color_primary": "#1A1A1A", "color_primary_bg": "#F5F5F5",
        "color_page_bg": "#FFFFFF", "color_ink": "#1A1A1A",
    },
    "beauty": {
        "color_primary": "#D4698B", "color_primary_bg": "#FFF0F5",
        "color_page_bg": "#FFFAF8", "color_ink": "#2E2E2E",
    },
    "food": {
        "color_primary": "#4A7C6F", "color_primary_bg": "#E8F4F0",
        "color_page_bg": "#FFFDF7", "color_ink": "#2C2A3A",
    },
    "electronics": {
        "color_primary": "#2563EB", "color_primary_bg": "#EFF6FF",
        "color_page_bg": "#F8FAFC", "color_ink": "#1E293B",
    },
    "health": {
        "color_primary": "#059669", "color_primary_bg": "#ECFDF5",
        "color_page_bg": "#F0FDF4", "color_ink": "#1E293B",
    },
    "home": {
        "color_primary": "#92400E", "color_primary_bg": "#FEF3C7",
        "color_page_bg": "#FFFBEB", "color_ink": "#292524",
    },
    "pets": {
        "color_primary": "#F59E0B", "color_primary_bg": "#FEF9C3",
        "color_page_bg": "#FFFBEB", "color_ink": "#292524",
    },
    "default": {
        "color_primary": "#6B5FD4", "color_primary_bg": "#F0EDFF",
        "color_page_bg": "#FBF8F4", "color_ink": "#2C2A3A",
    },
}


def _pick_palette(niche: str) -> Dict[str, str]:
    """Return the best colour palette for a given niche keyword."""
    niche_lower = niche.lower()
    for key in _NICHE_PALETTES:
        if key in niche_lower:
            return _NICHE_PALETTES[key]
    return _NICHE_PALETTES["default"]


def _currency_symbol(currency: str) -> str:
    symbols: Dict[str, str] = {
        "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
        "AUD": "A$", "CAD": "C$", "JPY": "¥", "CNY": "¥",
        "AED": "AED ", "SGD": "S$",
    }
    return symbols.get(currency.upper(), currency.upper() + " ")


def _generate_theme(
    store_name: str,
    store_idea: str,
    niche: str,
    target_audience: str,
    currency: str,
    country: str,
    free_shipping_threshold: str,
    whatsapp_number: str,
    unique_selling_points: List[str],
    brand_story: str,
    tagline: str,
    hero_headline: str,
    primary_collection: str,
) -> Dict[str, Any]:
    """Build a full CRO-optimised Shopify theme configuration from store info.

    Returns a dict with keys for every theme file that needs to be created or
    updated.  Every section is fully linked, conversion-rate-optimised, and
    uses Shopify default features where appropriate.
    """
    sym = _currency_symbol(currency)
    palette = _pick_palette(niche)

    # --- Derived copy --------------------------------------------------
    usp1 = unique_selling_points[0] if len(unique_selling_points) > 0 else "Premium quality"
    usp2 = unique_selling_points[1] if len(unique_selling_points) > 1 else "Fast shipping"
    usp3 = unique_selling_points[2] if len(unique_selling_points) > 2 else "Easy returns"

    announcement_1 = f"🚚 Free delivery above {sym}{free_shipping_threshold} · {country}-wide"
    announcement_2 = f"⭐ Trusted by 10,000+ happy customers"
    announcement_3 = f"🎁 {usp1}"

    wa_link = f"https://wa.me/{whatsapp_number.lstrip('+').replace(' ', '').replace('-', '')}" if whatsapp_number else ""

    # --- config/settings_data.json -------------------------------------
    settings_data = {
        "current": {
            **palette,
            "type_header_font": "nunito_n4",
            "type_body_font": "nunito_n4",
        },
        "presets": {},
    }

    # --- sections/header-group.json ------------------------------------
    header_group = {
        "type": "header",
        "name": "Header",
        "sections": {
            "announcement-bar": {
                "type": "kc-announcement-bar-v2",
                "settings": {
                    "message_1": announcement_1,
                    "message_2": announcement_2,
                    "message_3": announcement_3,
                },
            }
        },
        "order": ["announcement-bar"],
    }

    # --- sections/footer-group.json ------------------------------------
    footer_group = {
        "type": "footer",
        "name": "Footer",
        "sections": {
            "footer": {
                "type": "kc-footer-v2",
                "settings": {},
            }
        },
        "order": ["footer"],
    }

    # --- templates/index.json ------------------------------------------
    index_template = {
        "sections": {
            "announcement-bar": {
                "type": "kc-announcement-bar-v2",
                "settings": {
                    "message_1": announcement_1,
                    "message_2": announcement_2,
                    "message_3": announcement_3,
                },
            },
            "hero": {
                "type": "kc-hero-v2",
                "settings": {
                    "headline": hero_headline,
                    "subheading": tagline,
                    "cta1_text": f"Shop now →",
                    "cta1_url": f"/collections/{primary_collection}",
                    "cta2_text": "Browse all",
                    "cta2_url": "/collections/all",
                    "proof1": "10,000+ happy customers",
                    "proof2": usp1,
                    "proof3": f"Free shipping above {sym}{free_shipping_threshold}",
                },
            },
            "best-sellers": {
                "type": "kc-best-sellers-v2",
                "settings": {
                    "heading": "Our best sellers",
                    "collection": primary_collection or "frontpage",
                    "products_to_show": 8,
                },
            },
            "brand-story": {
                "type": "kc-brand-story-v2",
                "settings": {
                    "heading": f"The {store_name} story",
                    "body_text": f"<p>{brand_story}</p>",
                },
            },
            "ugc-gallery": {
                "type": "kc-ugc-gallery-v2",
                "settings": {
                    "heading": "Loved by real customers",
                    "subheading": "See what people are saying",
                },
            },
            "trust-strip": {
                "type": "kc-trust-strip-v2",
                "settings": {},
            },
            "email-signup": {
                "type": "kc-email-signup-v2",
                "settings": {
                    "heading": f"Get {sym}100 off your first order",
                    "subheading": f"Join thousands of happy customers getting updates from {store_name}.",
                    "placeholder": "Enter your email address",
                    "button_text": f"Claim {sym}100 off",
                    "privacy_note": "No spam. Unsubscribe anytime.",
                },
            },
        },
        "order": [
            "announcement-bar",
            "hero",
            "best-sellers",
            "brand-story",
            "ugc-gallery",
            "trust-strip",
            "email-signup",
        ],
    }

    # --- templates/product.json ----------------------------------------
    product_hooks = [
        f'"Love this product — exactly what I needed!" — Happy Customer',
        f'"Best purchase I\'ve made this year." — Verified Buyer',
        f'"Amazing quality. Will order again!" — Repeat Customer',
        f'"{usp1} — totally worth it." — Reviewer',
        f'"Arrived fast and beautifully packaged!" — 5-Star Review',
        f'"{usp2} — highly recommend {store_name}." — Loyal Customer',
        f'"Worth every penny." — Top Reviewer',
        f'"Finally found what I was looking for!" — New Customer',
    ]

    product_template = {
        "sections": {
            "gallery": {
                "type": "kcp-gallery-v4",
                "settings": {},
            },
            "product-info": {
                "type": "kcp-product-info-v4",
                "settings": {
                    **{f"hook_{i+1}": hook for i, hook in enumerate(product_hooks)},
                },
            },
            "action-zone": {
                "type": "kcp-action-zone-v4",
                "settings": {},
            },
            "offers": {
                "type": "kcp-offers-v4",
                "settings": {},
            },
            "video-grid": {
                "type": "kcp-video-grid-v4",
                "settings": {},
            },
            "product-tabs": {
                "type": "kcp-product-tabs-v4",
                "settings": {},
            },
            "why-parents": {
                "type": "kcp-why-parents-v4",
                "settings": {},
            },
            "reviews": {
                "type": "kcp-reviews-v4",
                "settings": {},
            },
            "faq": {
                "type": "kcp-faq-v4",
                "settings": {},
            },
            "policy-grid": {
                "type": "kcp-policy-grid-v4",
                "settings": {},
            },
            "sticky-atc": {
                "type": "kcp-sticky-atc-v4",
                "settings": {},
            },
            "floating-wa": {
                "type": "kcp-floating-wa-v4",
                "settings": {},
            },
        },
        "order": [
            "gallery",
            "product-info",
            "action-zone",
            "offers",
            "video-grid",
            "product-tabs",
            "why-parents",
            "reviews",
            "faq",
            "policy-grid",
            "sticky-atc",
            "floating-wa",
        ],
    }

    # --- Shopify-default templates ------------------------------------
    default_templates = {
        "templates/404.json": {
            "sections": {"main": {"type": "main-404", "settings": {}}},
            "order": ["main"],
        },
        "templates/article.json": {
            "sections": {"main": {"type": "main-article", "settings": {}}},
            "order": ["main"],
        },
        "templates/blog.json": {
            "sections": {"main": {"type": "main-blog", "settings": {}}},
            "order": ["main"],
        },
        "templates/cart.json": {
            "sections": {"cart": {"type": "kc-cart-page-v2", "settings": {}}},
            "order": ["cart"],
        },
        "templates/collection.json": {
            "sections": {"main": {"type": "main-collection", "settings": {}}},
            "order": ["main"],
        },
        "templates/page.json": {
            "sections": {"main": {"type": "main-page", "settings": {}}},
            "order": ["main"],
        },
        "templates/password.json": {
            "sections": {
                "main": {"type": "main-password-header", "settings": {}},
                "footer": {"type": "main-password-footer", "settings": {}},
            },
            "order": ["main", "footer"],
        },
        "templates/search.json": {
            "sections": {"main": {"type": "main-search", "settings": {}}},
            "order": ["main"],
        },
    }

    # --- Full output ---------------------------------------------------
    return {
        "store_name": store_name,
        "store_idea": store_idea,
        "niche": niche,
        "cro_features": [
            "Rotating announcement bar with free-shipping & social-proof messages",
            "Hero section with primary CTA, secondary CTA, and proof strip",
            "Best-sellers carousel with quick-add-to-cart (zero-page-reload)",
            "Brand story section for emotional connection",
            "UGC / social-proof gallery",
            "Trust badge strip (safety, delivery, returns, COD)",
            "Email capture with first-order discount incentive",
            "Product page: rotating review hooks, urgency badges, sold-count social proof",
            "Product page: EMI breakdown for high-ticket items",
            "Sticky add-to-cart bar on product pages",
            "Floating WhatsApp button for instant support",
            "FAQ accordion to reduce purchase hesitation",
            "Policy grid (shipping, returns, warranty) on product page",
        ],
        "files": {
            "config/settings_data.json": settings_data,
            "sections/header-group.json": header_group,
            "sections/footer-group.json": footer_group,
            "templates/index.json": index_template,
            "templates/product.json": product_template,
            **default_templates,
        },
        "whatsapp_link": wa_link,
        "instructions": (
            "1. Use shopify_apply_theme to push these files to your active theme.\n"
            "2. Customise images via the Shopify Theme Editor → each section has an image_picker setting.\n"
            "3. Create collections matching your primary_collection handle.\n"
            "4. Add products with tags 'best-seller' to populate the best-sellers section.\n"
            "5. Set product metafields (custom.review_score, custom.review_count, custom.sold_count) for social proof.\n"
            "6. Update social media links in Theme Settings → Social Media."
        ),
    }


class GenerateWebsiteInput(BaseModel):
    """Input for the website-generation tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    store_name: str = Field(
        ..., min_length=1,
        description="Store / brand name, e.g. 'Kynd Cubs'",
    )
    store_idea: str = Field(
        ..., min_length=5,
        description=(
            "Brief description of the store concept, e.g. "
            "'Curated educational toys for Indian kids aged 1-8'"
        ),
    )
    niche: str = Field(
        default="default",
        description=(
            "Product niche for colour-palette selection. "
            "Options: kids, fashion, beauty, food, electronics, health, home, pets, default"
        ),
    )
    target_audience: str = Field(
        default="general consumers",
        description="Who this store is for, e.g. 'Indian parents of toddlers'",
    )
    currency: str = Field(
        default="INR",
        description="ISO 4217 currency code, e.g. INR, USD, EUR",
    )
    country: str = Field(
        default="India",
        description="Primary country for shipping copy",
    )
    free_shipping_threshold: str = Field(
        default="499",
        description="Minimum order value for free shipping (number only)",
    )
    whatsapp_number: str = Field(
        default="",
        description="WhatsApp number with country code, e.g. +917509802310",
    )
    unique_selling_points: Optional[List[str]] = Field(
        default=None,
        description="Up to 3 USPs, e.g. ['Safety tested', 'Pan-India delivery', 'Easy returns']",
    )
    brand_story: str = Field(
        default="",
        description="1-3 sentence brand story for the homepage",
    )
    tagline: str = Field(
        default="",
        description="Store tagline / subheading for the hero section",
    )
    hero_headline: str = Field(
        default="",
        description=(
            "Hero banner headline. Use [em]...[/em] for emphasis. "
            "e.g. 'Toys your child [em]won't ignore[/em]'"
        ),
    )
    primary_collection: str = Field(
        default="frontpage",
        description="Handle of the main collection to feature, e.g. 'frontpage' or 'best-sellers'",
    )


@mcp.tool(
    name="shopify_generate_website",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def shopify_generate_website(params: GenerateWebsiteInput) -> str:
    """Generate a complete CRO-optimised Shopify website theme from a store
    idea and basic information.

    Returns a full set of theme configuration files (templates, sections,
    config) populated with conversion-rate-optimised copy, fully linked
    sections, and Shopify default features.

    After generation, use shopify_apply_theme to push the files to your store.
    """
    try:
        usps = params.unique_selling_points or ["Premium quality", "Fast shipping", "Easy returns"]

        brand_story = params.brand_story or (
            f"{params.store_name} was built to solve a simple problem — "
            f"finding great {params.niche} products shouldn't be hard. "
            f"We hand-pick every item so you don't have to."
        )
        tagline = params.tagline or f"Curated {params.niche} products for {params.target_audience}."
        hero_headline = params.hero_headline or f"The [em]best {params.niche}[/em] products, curated for you"

        result = _generate_theme(
            store_name=params.store_name,
            store_idea=params.store_idea,
            niche=params.niche,
            target_audience=params.target_audience,
            currency=params.currency,
            country=params.country,
            free_shipping_threshold=params.free_shipping_threshold,
            whatsapp_number=params.whatsapp_number,
            unique_selling_points=usps,
            brand_story=brand_story,
            tagline=tagline,
            hero_headline=hero_headline,
            primary_collection=params.primary_collection,
        )
        return _fmt(result)
    except Exception as e:
        return _error(e)


# ═══════════════════════════════════════════════════════════════════════════
# THEME MANAGEMENT — PUSH GENERATED THEME TO SHOPIFY
# ═══════════════════════════════════════════════════════════════════════════

class ListThemesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="shopify_list_themes",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shopify_list_themes(params: ListThemesInput) -> str:
    """List all themes on the store. Use this to find the active theme ID
    before calling shopify_apply_theme."""
    try:
        data = await _request("GET", "themes.json")
        themes = data.get("themes", [])
        return _fmt({"count": len(themes), "themes": themes})
    except Exception as e:
        return _error(e)


class ApplyThemeInput(BaseModel):
    """Push generated theme files to a Shopify theme via the Asset API."""
    model_config = ConfigDict(extra="forbid")

    theme_id: int = Field(
        ...,
        description=(
            "Shopify theme ID to update. Use shopify_list_themes to find the "
            "active theme ID."
        ),
    )
    files: Dict[str, Any] = Field(
        ...,
        description=(
            "Dict of file paths → JSON content from shopify_generate_website "
            "output's 'files' key, e.g. "
            '{"config/settings_data.json": {...}, "templates/index.json": {...}}'
        ),
    )


@mcp.tool(
    name="shopify_apply_theme",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shopify_apply_theme(params: ApplyThemeInput) -> str:
    """Push theme files to a Shopify theme using the Asset API.

    Takes the 'files' dict from shopify_generate_website output and uploads
    each file as a theme asset.  Use shopify_list_themes first to find the
    correct theme_id.
    """
    try:
        results: List[Dict[str, str]] = []
        for file_path, content in params.files.items():
            asset_key = file_path
            asset_value = json.dumps(content, indent=2, ensure_ascii=False)
            body = {
                "asset": {
                    "key": asset_key,
                    "value": asset_value,
                }
            }
            try:
                await _request(
                    "PUT",
                    f"themes/{params.theme_id}/assets.json",
                    body=body,
                )
                results.append({"file": asset_key, "status": "uploaded"})
                logger.info(f"Uploaded theme asset: {asset_key}")
            except Exception as file_err:
                results.append({"file": asset_key, "status": "failed", "error": str(file_err)})
                logger.error(f"Failed to upload {asset_key}: {file_err}")

        uploaded = sum(1 for r in results if r["status"] == "uploaded")
        failed = sum(1 for r in results if r["status"] == "failed")
        return _fmt({
            "theme_id": params.theme_id,
            "total_files": len(results),
            "uploaded": uploaded,
            "failed": failed,
            "details": results,
        })
    except Exception as e:
        return _error(e)


class GetThemeAssetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme_id: int = Field(..., description="Shopify theme ID")
    asset_key: str = Field(
        ...,
        description="Asset key / file path, e.g. 'templates/index.json'",
    )


@mcp.tool(
    name="shopify_get_theme_asset",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shopify_get_theme_asset(params: GetThemeAssetInput) -> str:
    """Read a single asset from a Shopify theme. Useful for inspecting
    current theme files before applying changes."""
    try:
        data = await _request(
            "GET",
            f"themes/{params.theme_id}/assets.json",
            params={"asset[key]": params.asset_key, "theme_id": params.theme_id},
        )
        return _fmt(data.get("asset", data))
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
