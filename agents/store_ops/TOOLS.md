---
tools: [list_products, create_product, activate_product, update_price, add_license_keys, get_inventory, list_orders,
        get_order, deliver_order, send_email, read_company_doc, recall_memory, remember, request_account_need, whop_request]
server_tools: []
---
# Store Ops tools
Catalogue and inventory tools via the active StoreAdapter (mock until the CEO's platform is provisioned).

`whop_request` exists only with STORE_ADAPTER=whop: one allowlisted Whop operation per call (products, variants, promo codes, checkout links, store preferences). Publishing, archiving and preferences wait for a human; prices go through `update_price`, refunds through `refund_order`.
